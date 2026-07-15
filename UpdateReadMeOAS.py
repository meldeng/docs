from __future__ import print_function
import base64
import boto3
from botocore.client import ClientError
from collections import defaultdict, OrderedDict
import copy
import io
import json
import os
import re
import requests
import sys
import yaml
import argparse

# Global variables that will be initialized in main()
AWS_REGION = None
ENV_AUTH_HEADER = None
ENV_CONFIG_BUCKET = None
ENV_CONFIG_FILE = None

'''
Get the name of the service we want to update the ReadMe docs for and the Meld
environment to do it in.

In the Event that invokes this Lambda function, the value of `codepipeline` has
the format:  <service-name>-<env>.  We right split (i.e rear of string) on '-'
to get the 'service-name' & 'env'.
'''
def get_service(event):
    codepipeline = event['detail']['pipeline']
    codepipeline = codepipeline.strip()
    print(f"Source CodePipeline: {codepipeline}")

    res = codepipeline.rsplit('-', 1)

    return res[0], res[1]


'''
Retreive the Service Provider Properties from Meld's SPP API to generate the sorted lists of
service providers, and Payment Method's type & subtype.  These are then used to replace components'
values that contain all the properties existing in the code, but we want so show only properties the
firm is formally supporting.  (i.e. Circle (crypto) or Gift Cards)

:return: Return sorted lists for a) service providers, b) paymentMethod Types & c) paymentMethod Subtypes
'''
def get_service_providers_properties(env):
    url = f"https://api-{env}.meld.io/service-providers/properties"

    response = requests.get( url=url )

    if response.status_code == 200:
        print(f"SUCCESS: retrieved Meld SPP's payment methods.")
        serviceProviders = []
        Type = {}
        SubType = {}
        Types = []
        SubTypes = []

        service_providers = response.json()
        for sp in service_providers:
            serviceProviders.append( sp['serviceProvider'] )

            for payMeth in sp['paymentMethods']:
                Type[ payMeth['type'] ] = True
                SubType[ payMeth['subtype'] ] = True

        Types = list( Type.keys() )
        SubTypes = list( SubType.keys() )

    else:
        print(f"ERROR: retrieving Meld SPP's payment methods| Code: {response.status_code}|{response.text}")
        sys.exit()

    return serviceProviders.sort(), Types.sort(), SubTypes.sort()


'''
Get the necessary config values based on which CodePipeline has invoked this
Lambda function.

Read the config file from an S3 bucket, load it as a YAML file, then select
the values needed for the specific service & env.
'''
def get_configs(service):
    try:
        with open(ENV_CONFIG_FILE, 'r') as f:
            config_content = yaml.load(f, Loader=yaml.FullLoader)

        # Must use `defaultdict` as we dynamically add multi-level dictionaries
        # in the FOR loop below
        configs = defaultdict(lambda: defaultdict(dict))

        # IF ths service is not in the config file
        #   Return an empty dict
        if service not in config_content:
            print(f"WARN: Meld {service} is not in config file.")
            return configs

        # A service's OAS may generate multiple API sections in ReadMe
        for section in config_content[service]:
            for version in config_content[service][section]['versions']:
                configs[section][version]['security']        = config_content['security']
                configs[section][version]['securitySchemes'] = config_content['securitySchemes']
                configs[section][version]['servers']         = config_content['servers']
                configs[section][version]['info_title']      = section
                configs[section][version]['tags']            = config_content[service][section]['tags']
                configs[section][version]['meld_oas']        = config_content[service][section]['meld_oas']
                configs[section][version]['readme_oas']      = config_content[service][section]['versions'][version]['readme_oas']
                # MUST use get() cuz 'paths_to_keep', 'paths_to_remove' & 'components_to_modify' may not exist in the config file & 
                # need to return an empty dict so code looping on this will not throw Exception
                configs[section][version]['paths_to_keep']        = config_content[service][section]['versions'][version].get('paths_to_keep', {})
                configs[section][version]['paths_to_remove']      = config_content[service][section]['versions'][version].get('paths_to_remove', {})
                configs[section][version]['components_to_modify'] = config_content[service][section]['versions'][version].get('components_to_modify', {})
                configs[section][version]['components_defaults']  = config_content[service][section]['versions'][version].get('components_defaults', {})
                configs[section][version]['output_file_name']     = section.lower().replace(' ', '') + "-" + version.lower().replace('-', '')

                # *Need* to do a deepcopy(), as the following IF-ELSE will modify the value, but we
                # want the next iteration in the loop to get a copy of the original value.
                configs[section][version]['x-readme'] = copy.deepcopy(config_content['x-readme'])

                if version == 'unversioned':
                    # We don't want to show the static headers for the 'unversioned' version.
                    # Can *not* del the 'headers' as it seems it's not a "known" key that is
                    # "set" when dynamically creating the multi-level dict.  So, next best
                    # thing is to set the value to be a blank array.
                    configs[section][version]['x-readme']['headers'] = []
                else:
                    configs[section][version]['x-readme']['headers'][0]['value'] = version

        return configs
    except Exception as e:
        print(f"ERROR: config file missing or invalid!|Error: {e}")
        sys.exit()


'''
Get the Meld OAS file from the service that was just deployed
via <service>-http's API endpoint.

:return: Return the OAS file as a Python dictionary.
'''
def get_meld_oas_file(env, configs, version, service):
    url = f"https://api-{env}.meld.io/oas-{configs['meld_oas']}/{version}"
    print(f"Requesting OAS from URL: {url}")

    response = requests.get( url=url )

    if response.status_code == 200:
        print(f"SUCCESS: retrieved Meld {service} OAS file for {version}. from: {url}")
    elif response.status_code == 404 or response.status_code == 500:
        print(f"WARN: no Meld {service} OAS file for {version}| Code: {response.status_code}|{response.text}")
        # This scenario may be where the service does NOT have this OAS version.  We return an empty dynamic
        # multi-level dict to go thru the rest of the flow & generate an OAS file from the latest existing version
        # *but* replace the `Meld-Version` static header with this version.
        return defaultdict(lambda: defaultdict(dict))
    else:
        print(f"ERROR: retrieving Meld {service} OAS file for {version}| Code: {response.status_code}|{response.text}")
        sys.exit()

    return response.json()


'''
A recurisve function to return a JSON block inside the overall JSON doc that
has the property we're looking for.

:param d: Dictionary ("parent" JSON block) we're trying to extract a specific sub-block from
:param ks: List of the path hierarchy to a property (i.e '{ x: { y: { z: <val to update>}} } ' => ['x', 'y', 'z'])

:return: A JSON block that contains the property we're looking for
'''
def get_json_block(d, ks): 
  head, *tail = ks   # equivalent to head = ks[0], tail = [1:]
  return get_json_block(d.get(head, {}), tail) if tail else d


'''
Update the OAS file to be uploaded to ReadMe.
• Add/update JSON blocks that are ReadMe specific.
• Remove API EPs that don't need to be publisehd.
• Update components with values to show customers.
  - Often the OAS uses an enum from the code that contains many more
    SP/Payment Methods than Meld actually officially support.  These
    may be in the codebase from previous exploratory Proof-of-Concepts.

:return: The modified OAS file as a Python dictionary.
'''
def prep_meld_oas_file(meld_oas_file, configs):
    meld_oas_file['info']['title']                 = configs['info_title']
    meld_oas_file['info']['description']           = ''
    meld_oas_file['servers']                       = configs['servers']
    meld_oas_file['tags']                          = configs['tags']
    meld_oas_file['x-readme']                      = configs['x-readme']
    meld_oas_file['components']['securitySchemes'] = configs['securitySchemes']

    # NOTE: Make a LIST of all the keys of the 'paths' & '[paths][path]' JSON block, cuz
    # if just used `.items()` the loop will throw an exception when we del certain blocks
    #
    # FOR loop thru each API EP of the OAS file
    #   IF it is one that we want to publish
    #     FOR loop thru the HTTP methods of the API EP
    #       IF the method is one we want to publish
    #         Add the 'security' block to this method's block
    #         Set the 'tags' value to this method's block
    #
    #         IF the method's operationId string has an "_" (indicating multiple Java versions of EP)
    #           Split the operationId string on "_"
    #           Set the operationId string to only the 'slug'
    #       ELSE
    #         Delete the method's block
    #   ELSE
    #     Delete the API EP's block
    # 
    for path in list(meld_oas_file['paths'].keys()):
        if path in configs['paths_to_keep'].keys():
            for method in list(meld_oas_file['paths'][path].keys()):
                if method in configs['paths_to_keep'][path]['methods']:
                    # Preserve the original responses and their order
                    responses = meld_oas_file['paths'][path][method].get('responses', {})
                    
                    # Only convert 5xx to 500 for ReadMe compatibility
                    if '5xx' in responses:
                        responses['500'] = responses.pop('5xx')
                    
                    meld_oas_file['paths'][path][method]['responses'] = responses
                    meld_oas_file['paths'][path][method]['security'] = configs['security']
                    meld_oas_file['paths'][path][method]['tags'] = configs['paths_to_keep'][path]['tags']
                    
                    # Add explicit example for widget endpoint to exclude authenticationBypassDetails
                    # Always override to ensure authenticationBypassDetails is never included
                    if path == '/crypto/session/widget' and method == 'post':
                        request_body = meld_oas_file['paths'][path][method].get('requestBody', {})
                        if request_body and 'content' in request_body and 'application/json' in request_body['content']:
                            content = request_body['content']['application/json']
                            # Always set example to exclude authenticationBypassDetails
                            content['example'] = {
                                "sessionType": "BUY",
                                "sessionData": {
                                    "serviceProvider": "TOPPER",
                                    "countryCode": "US",
                                    "sourceCurrencyCode": "USD",
                                    "sourceAmount": "100",
                                    "destinationCurrencyCode": "BTC",
                                    "walletAddress": "bc1qr74wmrcwqq9w5yxczxj6udts9mnqsh3xlhk5yp"
                                },
                                "externalSessionId": "example-session-id",
                                "externalCustomerId": "example-customer-id"
                            }
                    
                    # Remove version suffixes from operationId (handles both underscore and hyphen formats)
                    operation_id = meld_oas_file['paths'][path][method]['operationId']
                    # Handle underscore format: operationId_version -> operationId
                    if re.search("_", operation_id) != None:
                        slug, version = operation_id.split("_", 1)
                        meld_oas_file['paths'][path][method]['operationId'] = slug
                    # Handle hyphen format: operationId-YYYYMMDD -> operationId (e.g., /crypto-quote-get-20260203 -> /crypto-quote-get)
                    elif re.search(r"-\d{8}$", operation_id) != None:
                        # Remove trailing hyphen followed by 8 digits (YYYYMMDD format)
                        meld_oas_file['paths'][path][method]['operationId'] = re.sub(r"-\d{8}$", "", operation_id)
                else:
                    del meld_oas_file['paths'][path][method]
        else:
            del meld_oas_file['paths'][path]

    # FOR loop thru each components schema we want to modify
    #   FOR each property of a component
    #     Create a list of the path hierarchy to the property to modify
    #     Get the reference to the JSON block that contains the property to modify
    #     Update the property in the JSON block
    #
    for schema, properties in configs['components_to_modify'].items():
        # Check if the schema exists in the OAS file
        if schema not in meld_oas_file['components']['schemas']:
            print(f"WARN: Schema '{schema}' not found in OAS file - skipping component modifications")
            continue
            
        for prop in properties:
            property_path = prop['path'].split('.')
            sub_block_to_update = get_json_block( meld_oas_file['components']['schemas'][schema], property_path )
            sub_block_to_update[ property_path[-1].replace("'", "") ] = prop['replace_with']

    # FOR loop thru each components schema we want to set default values for
    #   FOR each property default configuration
    #     Create a list of the path hierarchy to the property to set default
    #     Get the reference to the JSON block that contains the property
    #     Set the default value in the JSON block
    #
    # Apply default values to schemas
    apply_defaults_to_schemas(meld_oas_file, configs)

    return meld_oas_file


'''
Helper function to apply default values to schemas in an OAS file.
'''
def apply_defaults_to_schemas(meld_oas_file, defaults_config):
    """Apply default values to matching schemas (including versioned variants)."""
    if not defaults_config or 'components_defaults' not in defaults_config:
        return
    
    for schema, defaults in defaults_config['components_defaults'].items():
        # Find all schemas that match the base schema name (including versioned variants)
        matching_schemas = []
        if schema in meld_oas_file['components']['schemas']:
            matching_schemas.append(schema)
        
        # Also check for versioned schemas (e.g., ConnectStartRequest_2022_11_10)
        for schema_name in meld_oas_file['components']['schemas'].keys():
            if schema_name.startswith(schema + '_'):
                matching_schemas.append(schema_name)
        
        if not matching_schemas:
            continue
        
        # Apply defaults to all matching schemas (base and versioned)
        for matching_schema in matching_schemas:
            for default_config in defaults:
                property_path = default_config['path'].split('.')
                try:
                    sub_block_to_update = get_json_block(meld_oas_file['components']['schemas'][matching_schema], property_path)
                    sub_block_to_update[property_path[-1]] = default_config['value']
                    # Also set example to the same value for ReadMe to prefill the field
                    property_obj_path = property_path[:-1]
                    property_obj = get_json_block(meld_oas_file['components']['schemas'][matching_schema], property_obj_path)
                    property_obj['example'] = default_config['value']
                except Exception as e:
                    print(f"WARN: Failed to set default for {matching_schema}.{'.'.join(property_path)}: {e}")


'''
Order the display of API endpoints shown in the API Reference page.
• Add/update JSON blocks that are ReadMe specific.

:return: The modified OAS file as a Python dictionary.
'''
def order_meld_oas_file(meld_oas_file, configs):
    # CRITICAL: we are relying on Python3.7+ Dictionary default behavior of
    # ordering it's keys based on sequence of insertions!  Not just in this
    # function but also in the get_configs() where the order in the YML file
    # is preserved when read in.
    #
    # FOR loop thru each API EP of the configs (i.e. those we want to keep)
    #   FOR loop thru the HTTP methods of the API EP (specified in the configs)
    #     Copy into the placeholder Dict the values from the OAS file. This is
    #     how we can set the display order we want that is defined in the configs.
    #
    # Set the (OAS file) API EP & methods to be the ordered version
    # 

    # Must use `defaultdict` as we dynamically add multi-level dictionaries
    # in the FOR loop below
    ordered_methods = defaultdict(dict)

    for path in configs['paths_to_keep'].keys():
        for method in configs['paths_to_keep'][path]['methods']:
            # Check if the path and method exist in the OAS file
            if path in meld_oas_file['paths'] and method in meld_oas_file['paths'][path]:
                # Copy the method data but preserve the tags that were set in prep_meld_oas_file
                method_data = meld_oas_file['paths'][path][method].copy()
                # Ensure the tags are set correctly from the config
                method_data['tags'] = configs['paths_to_keep'][path]['tags']
                ordered_methods[path][method] = method_data
            else:
                print(f"WARN: Method '{method}' not found in path '{path}' for version - skipping")

    meld_oas_file['paths'] = ordered_methods

    return meld_oas_file


def purge_prev_oas_file(prev_oas_file, configs):
    for path in configs['paths_to_remove'].keys():
        if path not in prev_oas_file['paths']:
            print(f"WARN: purge skipped — path '{path}' not in prev OAS file")
            continue
        for method in configs['paths_to_remove'][path]['methods']:
            if method not in prev_oas_file['paths'][path]:
                print(f"WARN: purge skipped — method '{method}' not in '{path}' in prev OAS file")
                continue
            del prev_oas_file['paths'][path][method]

        # If we deleted all the path's methods
        #   Delete the path
        if len(prev_oas_file['paths'][path]) == 0:
            del prev_oas_file['paths'][path]

    return prev_oas_file


def merge_meld_oas_files(prev_oas_file, current_oas_file):
    for path in current_oas_file.get('paths', {}) or {}:
        for method in current_oas_file['paths'][path]:
            # Copy the method data but preserve the tags from the previous version
            method_data = current_oas_file['paths'][path][method].copy()
            if path in prev_oas_file['paths'] and method in prev_oas_file['paths'][path]:
                # Preserve the tags from the previous version (which should have the correct tags)
                method_data['tags'] = prev_oas_file['paths'][path][method]['tags']
            prev_oas_file['paths'][path][method] = method_data

    # Merge schemas - current file takes precedence, but preserve defaults/examples from current.
    # Some versioned OAS payloads return empty/partial components (e.g. no schemas key).
    current_schemas = (current_oas_file.get('components') or {}).get('schemas') or {}
    if current_schemas:
        prev_oas_file.setdefault('components', {}).setdefault('schemas', {})
        for schema in current_schemas.keys():
            # Current file's schema (which has defaults applied) takes precedence
            prev_oas_file['components']['schemas'][schema] = current_schemas[schema]

    if 'x-readme' in current_oas_file:
        prev_oas_file['x-readme'] = current_oas_file['x-readme']

    return prev_oas_file


'''
Get Meld's API creds to call ReadMe.

With the ReadMe API key, we use it like HTTP Basic Authentication, with the
key as the username and the password left blank (i.e. 'key:').  Then we
base64 this string to be used in the "Authorization:" header.
'''
def get_auth_creds():
    ssm = boto3.client('ssm', region_name=AWS_REGION)
    parameter = ssm.get_parameter(Name=ENV_AUTH_HEADER, WithDecryption=True)
    key = parameter['Parameter']['Value']

    key = key + ":"
    key_bytes = key.encode('ascii')
    base64_bytes = base64.b64encode(key_bytes)
    base64_key = base64_bytes.decode('ascii')

    return base64_key


'''
Upload to ReadMe the modified Meld OAS file.

Write the contents of the Meld OAS JSON doc to a file in a local directory.
Make a PUT call to ReadMe's API to upload the just written file.
If file upload was successful, upload the file to an S3 bucket.
'''
def update_readme(meld_oas_file, env, configs, s3):
    filename = f"oas-{configs['output_file_name']}.json"
    filename_local = os.path.join("output", filename)

    # Create output directory if it doesn't exist
    os.makedirs("output", exist_ok=True)

    # Remove authenticationBypassDetails from SessionTokenForWidgetRequest schemas
    # and ensure clientIpAddress is visible in SessionDataInputV20231219 schemas
    if 'components' in meld_oas_file and 'schemas' in meld_oas_file['components']:
        # Remove authenticationBypassDetails from request schemas
        for schema_name in ['SessionTokenForWidgetRequest', 'SessionTokenForWidgetRequestV20231219']:
            if schema_name in meld_oas_file['components']['schemas']:
                schema = meld_oas_file['components']['schemas'][schema_name]
                if 'properties' in schema and 'authenticationBypassDetails' in schema['properties']:
                    del schema['properties']['authenticationBypassDetails']
        
        # Ensure clientIpAddress is visible in SessionDataInputV20231219 and its subclasses
        session_data_schemas = ['SessionDataInputV20231219', 'CryptoBuySessionData', 'CryptoSellSessionData', 'CryptoTransferSessionData']
        for schema_name in session_data_schemas:
            if schema_name in meld_oas_file['components']['schemas']:
                schema = meld_oas_file['components']['schemas'][schema_name]
                if 'properties' in schema and 'clientIpAddress' in schema['properties']:
                    client_ip = schema['properties']['clientIpAddress']
                    # Remove hidden flag if present
                    if isinstance(client_ip, dict):
                        client_ip.pop('x-readme-hidden', None)
                        # Ensure it has a description
                        if 'description' not in client_ip or not client_ip['description']:
                            client_ip['description'] = "The client's IP address"

    # *Must* set `sort_keys=False` so the Dictionary content will be
    # written out to JSON in the same order as the meld_oas_file Dict's keys.
    # Convert the meld_oas_file dict to a JSON string
    json_str = json.dumps(meld_oas_file, sort_keys=False, indent=4, separators=(',', ': '), ensure_ascii=False)

    # Replace all occurrences of "5xx" with "500" because ReadMe does not support "5xx"
    json_str = json_str.replace('"5xx"', '"500"')

    # Write the modified JSON string to file
    with io.open(filename_local, 'w', encoding='utf-8') as f:
        f.write(json_str)

    # url = f"https://dash.readme.com/api/v1/api-specification/{configs['readme_oas']}"
    # header = { "Authorization": "Basic " + get_auth_creds() }

    # MUST use the 3-tuple format ('filename', fileobj, 'content_type') to upload file
    # to ReadMe.  Tried to use the 'with open(filename_tmp_local, 'rb') as f:' &
    # files={'spec': f} approach but that did not work.
    # files = {'spec': ('spec', open(filename_tmp_local, 'rb'), 'multipart/form-data')}

    # response = requests.put( url=url, headers=header, files=files )

    # if response.status_code == 200:
    #     print(f"SUCCESS: updated ReadMe {configs['meld_oas']} OAS file.")
    # else:
    #     print(f"ERROR: updating ReadMe {configs['meld_oas']} OAS file|Code: {response.status_code}|{response.text}")
    #     sys.exit()

    #s3.upload_file(filename_tmp_local, ENV_CONFIG_BUCKET, f"{env}/{filename}")

    return

def main():
    """
    Main function to run the script locally.
    Optional argument: specific API reference to process (e.g., 'payment-http', 'account-http')
    """
    import sys
    import argparse
    
    # Set up argument parser
    parser = argparse.ArgumentParser(description='Update ReadMe OAS files')
    parser.add_argument('--service', type=str, help='Specific service to process (e.g., payment, banklinking)')
    args = parser.parse_args()
    
    # Try to set up AWS session, but make it optional
    try:
        boto3.setup_default_session(profile_name="meld-devops-qa")
    except Exception:
        # AWS credentials not required for local OAS file generation
        pass
    
    env = "qa"
    
    # Set required environment variables if not already set
    if not os.getenv('AWS_REGION'):
        os.environ['AWS_REGION'] = 'us-west-2'  # Default region
    if not os.getenv('AUTH_HEADER'):
        os.environ['AUTH_HEADER'] = '/readme/api-key'  # Default SSM parameter path
    if not os.getenv('CONFIG_BUCKET'):
        os.environ['CONFIG_BUCKET'] = 'meld-readme-qa'  # Default config bucket
    if not os.getenv('CONFIG_FILE'):
        os.environ['CONFIG_FILE'] = 'readme.yml'  # Default config file

    # Initialize global variables
    global AWS_REGION, ENV_AUTH_HEADER, ENV_CONFIG_BUCKET, ENV_CONFIG_FILE
    AWS_REGION = os.getenv('AWS_REGION', 'ERR: missing!')
    ENV_AUTH_HEADER = os.getenv('AUTH_HEADER', 'ERR: missing!')
    ENV_CONFIG_BUCKET = os.getenv('CONFIG_BUCKET', 'ERR: missing!')
    ENV_CONFIG_FILE = os.getenv('CONFIG_FILE', 'ERR: missing!')

    # get configs used to det which OAS files to pull from ReadMe & Meld envs
    # S3 client is not actually used for file generation, so make it optional
    try:
        s3 = boto3.client('s3')
    except Exception:
        s3 = None  # Not needed for local file generation
    
    # Get all services from the config file
    try:
        with open(ENV_CONFIG_FILE, 'r') as f:
            config_content = yaml.load(f, Loader=yaml.FullLoader)
        
        # Get all services (keys in the config file that end with -http)
        services = [service for service in config_content.keys() if service.endswith('-http')]
        
        # If a specific service was provided, filter the services list
        if args.service:
            # Convert the provided service name to the expected format with -http suffix
            service_with_suffix = f"{args.service}-http"
            if service_with_suffix not in services:
                print(f"Error: Service '{args.service}' not found in config file")
                print(f"Available services: {', '.join([s.replace('-http', '') for s in services])}")
                sys.exit(1)
            services = [service_with_suffix]
        
        print(f"Processing {len(services)} services: {', '.join([s.replace('-http', '') for s in services])}")
        
        for service in services:
            print(f"\nProcessing service: {service.replace('-http', '')}")
            configs = get_configs(service)
            
            try:
                for section in configs:
                    print(f"\n-- {section} --\n")
                    prev_oas_file = None

                    # Get unversioned config for defaults that should apply to all versions
                    unversioned_config = configs[section].get('unversioned', {})
                    
                    for version in configs[section]:
                        print(f"section: {service}, version: {version}")
                        meld_oas_file = get_meld_oas_file(env, configs[section][version], version, service)

                        meld_oas_file = prep_meld_oas_file(meld_oas_file, configs[section][version])

                        meld_oas_file = order_meld_oas_file(meld_oas_file, configs[section][version])

                        if prev_oas_file is None:
                            prev_oas_file = meld_oas_file
                        else:
                            prev_oas_file = purge_prev_oas_file(prev_oas_file, configs[section][version])

                            meld_oas_file = merge_meld_oas_files(prev_oas_file, meld_oas_file)
                            # Re-apply defaults from unversioned config to ensure they're preserved through merges
                            apply_defaults_to_schemas(meld_oas_file, unversioned_config)
                            prev_oas_file = meld_oas_file

                        update_readme(meld_oas_file, env, configs[section][version], s3)

            except Exception as e:
                import traceback
                print(f"Error processing service {service}: {e}")
                print(f"Exception type: {type(e).__name__}")
                print(f"Full traceback:")
                traceback.print_exc()
                continue

        return
    except Exception as e:
        print(f"Error reading config file: {e}")
        raise e

if __name__ == '__main__':
    main()