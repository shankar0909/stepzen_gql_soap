import subprocess
import os

from tomlkit import key
from zeep import Client
from zeep.transports import Transport
from requests import Session
from textwrap import indent

API_NAME = "SOAP/my_first_stepzen"
BASE_FOLDER = "/Users/shankarsharma/Desktop/GQL/stepzen_gql_soap"


def run_stepzen_command(command, cwd=None):
    """Run a stepzen command and return output or raise error"""
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"[ERROR] StepZen command failed:\n{result.stderr}")
    return result.stdout


def init_stepzen_workspace():
    """Initialize a StepZen workspace non-interactively"""
    if not os.path.exists(os.path.join(BASE_FOLDER, ".stepzen")):
        print("[INFO] Initializing StepZen workspace...")
        subprocess.run(["stepzen", "init"], check=True)
    else:
        print("[INFO] StepZen workspace already exists.")

def deploy_stepzen():
    """Deploy StepZen endpoint"""
    print("[INFO] Deploying StepZen endpoint...")
    # run_stepzen_command(["stepzen", "deploy"], cwd=workspace_path)
    subprocess.run(["stepzen", "deploy", API_NAME, "--dir",BASE_FOLDER], check=True)

    print("[SUCCESS] Endpoint deployed.")


def map_xsd_to_graphql(xsd_str: str) -> str:
    x = xsd_str.lower()
    if "int" in x: return "Int!"
    if "float" in x or "double" in x or "decimal" in x: return "Float!"
    if "bool" in x: return "Boolean!"
    return "String!"

def build_postbody(action: str, soap_ns: str, args: list, soap_version: str = "soap12") -> str:
    """
    Build a SOAP envelope for POST body. Uses SOAP 1.2 by default.
    - action: SOAP operation (e.g., NumberToWords)
    - soap_ns: target namespace; omit if empty
    - args: argument names
    """
    if soap_version == "soap12":
        env_prefix = "soap12"
        env_ns = "http://www.w3.org/2003/05/soap-envelope"
    else:
        env_prefix = "soap"
        env_ns = "http://schemas.xmlsoap.org/soap/envelope/"

    # Opening tag for operation
    op_open = f'<{action} xmlns="{soap_ns}">' if soap_ns else f"<{action}>"

    # Arg lines with StepZen placeholder {{ .Get "arg" }}
    arg_lines = []
    for n in args:
        arg_lines.append(f'  <{n}>{{{{ .Get "{n}" }}}}</{n}>')
    arg_block = "\n".join(arg_lines)

    return f'''<?xml version="1.0" encoding="utf-8"?>
<{env_prefix}:Envelope xmlns:{env_prefix}="{env_ns}">
  <{env_prefix}:Body>
    {op_open}
{arg_block}
    </{action}>
  </{env_prefix}:Body>
</{env_prefix}:Envelope>'''

def generate_schema_and_index(wsdl_url: str, workspace_path: str):
    os.makedirs(workspace_path, exist_ok=True)

    # Set up Zeep
    session = Session()
    session.verify = True
    transport = Transport(session=session, timeout=12)
    client = Client(wsdl_url, transport=transport)

    schema_fields = []
    tns = getattr(client.wsdl, "tns", None) or ""
    seen_ops = set()
    
    for service in client.wsdl.services.values():
        for port in service.ports.values():
            endpoint = port.binding_options.get("address") or wsdl_url.split("?")[0]
            binding = port.binding

            for op in binding._operations.values():
                op_name = op.name
                if op_name in seen_ops:
                    continue  # skip duplicates
                seen_ops.add(op_name)
                arg_names = []
                gql_args_sig = []

                if op.input and op.input.body and op.input.body.type:
                    for el_name, el_type in op.input.body.type.elements:
                        arg_names.append(el_name)
                        gql_args_sig.append(f"{el_name}: {map_xsd_to_graphql(str(el_type))}")

                postbody_xml = build_postbody(op_name, tns, arg_names, "soap12")

                args_signature = ", ".join(gql_args_sig)
                if args_signature:
                    field_signature = f"{op_name}({args_signature})"
                else:
                    field_signature = op_name  # no parentheses if no args

                field = f'''  {field_signature} : JSON
    @rest(
      endpoint: "{endpoint}"
      method: POST
      headers: [
        {{name: "Content-Type", value: "text/xml"}},
        {{name: "Content-Type", value: "charset=utf-8"}}
      ]
      postbody: """
{indent(postbody_xml, "        ")}
      """
      transforms: [{{pathpattern: "[]", editor: "xml2json"}}]
      resultroot: "Envelope"
    )'''
                schema_fields.append(field)

    # Write schema.graphql
    schema_body = "type Query {\n" + "\n\n".join(schema_fields) + "\n}\n"
    with open(os.path.join(BASE_FOLDER, "schema.graphql"), "w") as f:
        f.write(schema_body)

    # Write index.graphql
    index_body = '''schema @sdl(files: ["schema.graphql"]) {
  query: Query
}
'''
    with open(os.path.join(BASE_FOLDER, "index.graphql"), "w") as f:
        f.write(index_body)

    print(f"[OK] Wrote schema.graphql and index.graphql in {workspace_path}")




if __name__ == "__main__":
    WSDL_URL = "https://soap-service-free.mock.beeceptor.com/CountryInfoService?WSDL"

    init_stepzen_workspace()
    #schema_file = generate_graphql_from_wsdl(WSDL_URL)
    generate_schema_and_index(WSDL_URL, BASE_FOLDER)
    deploy_stepzen()
