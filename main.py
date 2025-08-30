import os
import subprocess
import argparse
import json
from zeep import Client
from zeep.transports import Transport
from requests import Session
from textwrap import indent
import shutil


class StepZenSOAPGenerator:
    """
    Generates StepZen GraphQL schema from a WSDL URL.
    """

    def __init__(self, wsdl_url: str, api_name: str, base_folder: str):
        self.wsdl_url = wsdl_url
        self.api_name = api_name
        self.base_folder = base_folder
        self.complex_type_registry = {}  # Stores named GraphQL types

        # Setup SOAP client
        session = Session()
        session.verify = True
        transport = Transport(session=session, timeout=12)
        self.client = Client(wsdl_url, transport=transport)
        # Pick the first non-empty namespace from the WSDL
        self.tns = next((ns for prefix, ns in self.client.namespaces.items() if ns and prefix != "xsd"), "")

    # -----------------------------
    # Subprocess wrapper
    # -----------------------------
    @staticmethod
    def _run_stepzen_command(command, cwd=None):
        print(f"[DEBUG] Running command: {' '.join(command)}")
        print(f"[DEBUG] Working directory: {cwd}")
        print(f"[DEBUG] :", os.getcwd())
        result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"[ERROR] StepZen command failed:\n{result.stderr}")
        return result.stdout

    # -----------------------------
    # Workspace init / deploy
    # -----------------------------
    def init_workspace(self):
        """
        Initialize StepZen workspace if not already initialized.
        """
        try:
            print("[INFO] Attempting to initialize StepZen workspace...")
            self._run_stepzen_command(["stepzen", "init"], cwd=self.base_folder)
            print("[SUCCESS] StepZen workspace initialized.")
        except RuntimeError as e:
            if "already a StepZen workspace" in str(e):
                print("[INFO] StepZen workspace already exists. Skipping init.")
            else:
                raise

    def deploy(self):
        """
        Deploy StepZen endpoint.
        """
        #schemas_folder = os.path.join(self.base_folder, "schemas")
        #print("[INFO] Deploying StepZen endpoint...", self.api_name, " at ", cwd=self.base_folder)
        print("[INFO] Deploying StepZen endpoint...")
        moved = {}
        for f in ["index.graphql", "schema.graphql"]:
            print(f"[DEBUG] Looking for {f} to move to {os.getcwd()}")
            for root, _, files in os.walk(self.base_folder):
                print(f"[DEBUG] Checking in {root} for {f}")
                print(f"[DEBUG] Files found: {files}")
                if f in files :
                    src, dst = os.path.join(root, f), os.path.join(os.getcwd(), f)
                    print(f"[DEBUG] Moving {src} to {dst}")
                    shutil.move(src, dst)
                    moved[f] = src
                    break
        self._run_stepzen_command(["stepzen", "deploy", self.api_name], cwd=self.base_folder)
        for f, original in moved.items():
            shutil.move(os.path.join(os.getcwd(), f), original)
        print("[SUCCESS] Endpoint deployed.")

    # -----------------------------
    # Type mapping
    # -----------------------------
    def _map_xsd_to_graphql(self, el_type, type_name_hint="AutoType"):
        """
        Map a Zeep XSD type to a GraphQL type.
        """
        from zeep.xsd import ComplexType, BuiltinType

        # Handle built-in types
        if isinstance(el_type, BuiltinType):
            xsd_name = el_type.name.lower()
            gql_type = "String!"
            if "int" in xsd_name:
                gql_type = "Int!"
            elif "float" in xsd_name or "double" in xsd_name or "decimal" in xsd_name:
                gql_type = "Float!"
            elif "bool" in xsd_name:
                gql_type = "Boolean!"

        # Handle complex types
        elif isinstance(el_type, ComplexType):
            type_name = type_name_hint
            counter = 1
            while type_name in self.complex_type_registry:
                type_name = f"{type_name_hint}{counter}"
                counter += 1

            fields = []
            for name, sub_type in el_type.elements:
                sub_gql_type = self._map_xsd_to_graphql(sub_type, type_name_hint=name)
                # Optional fields
                min_occurs = getattr(sub_type, 'min_occurs', 1)
                if min_occurs == 0 and sub_gql_type.endswith("!"):
                    sub_gql_type = sub_gql_type.rstrip("!")
                # Arrays
                max_occurs = getattr(sub_type, 'max_occurs', 1)
                if max_occurs is None or max_occurs > 1:
                    sub_gql_type = f"[{sub_gql_type.rstrip('!')}]!"
                fields.append(f"{name}: {sub_gql_type}")

            self.complex_type_registry[type_name] = "type " + type_name + " {\n  " + "\n  ".join(fields) + "\n}"
            return type_name

        else:
            gql_type = "String!"

        return gql_type

    # -----------------------------
    # Build SOAP POST body
    # -----------------------------
    @staticmethod
    def _build_postbody(action: str, soap_ns: str, args: list, soap_version: str = "soap12") -> str:
        env_prefix = "soap12" if soap_version == "soap12" else "soap"
        env_ns = "http://www.w3.org/2003/05/soap-envelope" if soap_version == "soap12" else "http://schemas.xmlsoap.org/soap/envelope/"
        op_open = f'<{action} xmlns="{soap_ns}">' if soap_ns else f"<{action}>"
        arg_lines = [f'  <{n}>{{{{ .Get "{n}" }}}}</{n}>' for n in args]
        arg_block = "\n".join(arg_lines)

        return f'''<?xml version="1.0" encoding="utf-8"?>
<{env_prefix}:Envelope xmlns:{env_prefix}="{env_ns}">
  <{env_prefix}:Body>
    {op_open}
{arg_block}
    </{action}>
  </{env_prefix}:Body>
</{env_prefix}:Envelope>'''

    # -----------------------------
    # Build SDL field for StepZen
    # -----------------------------
    def _generate_field(self, op_name: str, arg_names: list, gql_args_sig: list, endpoint: str) -> str:
        postbody_xml = self._build_postbody(op_name, self.tns, arg_names, "soap12")
        args_signature = ", ".join(gql_args_sig)
        field_signature = f"{op_name}({args_signature})" if args_signature else op_name

        return f'''  {field_signature} : JSON
    @rest(
      endpoint: "{endpoint}"
      method: POST
      headers: [
        {{name: "Content-Type", value: "text/xml; charset=utf-8"}}
      ]
      postbody: """
{indent(postbody_xml, "        ")}
      """
      transforms: [{{pathpattern: "[]", editor: "xml2json"}}]
      resultroot: "Envelope"
    )'''

    # -----------------------------
    # Generate GraphQL schema + index
    # -----------------------------
    def generate_schema(self):
        os.makedirs(self.base_folder, exist_ok=True)
        schema_fields = []
        seen_ops = set()

        for service in self.client.wsdl.services.values():
            for port in service.ports.values():
                endpoint = port.binding_options.get("address") or self.wsdl_url.split("?")[0]
                binding = port.binding

                for op in binding._operations.values():
                    op_name = op.name
                    if op_name in seen_ops:
                        continue
                    seen_ops.add(op_name)

                    arg_names = []
                    gql_args_sig = []
                    if op.input and op.input.body and op.input.body.type:
                        for el_name, el_type in op.input.body.type.elements:
                            arg_names.append(el_name)
                            gql_args_sig.append(f"{el_name}: {self._map_xsd_to_graphql(el_type, type_name_hint=el_name)}")

                    field = self._generate_field(op_name, arg_names, gql_args_sig, endpoint)
                    schema_fields.append(field)

        types_body = "\n\n".join(self.complex_type_registry.values())
        schema_body = types_body + "\n\n" + "type Query {\n" + "\n\n".join(schema_fields) + "\n}\n"

        schema_path = os.path.join(self.base_folder, "schema.graphql")
        with open(schema_path, "w") as f:
            f.write(schema_body)

        index_body = '''schema @sdl(files: ["schema.graphql"]) {
  query: Query
}
'''
        index_path = os.path.join(self.base_folder, "index.graphql")
        with open(index_path, "w") as f:
            f.write(index_body)

        print(f"[OK] Wrote schema.graphql and index.graphql in {self.base_folder}")


# -----------------------------
# CLI entry point
# -----------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate StepZen GraphQL schemas from WSDL config")
    parser.add_argument("--config", required=True, help="JSON file mapping API_NAME -> WSDL_URL")
    parser.add_argument("--output", required=True, help="Base folder for StepZen workspaces")
    args = parser.parse_args()

    with open(args.config) as f:
        api_map = json.load(f)

    for api_name, wsdl_url in api_map.items():
        print(f"\n[INFO] Processing API '{api_name}' from WSDL '{wsdl_url}'")
        api_folder = os.path.join(args.output, api_name.replace("/", "_"))
        os.makedirs(api_folder, exist_ok=True)

        generator = StepZenSOAPGenerator(wsdl_url, api_name, api_folder)
        generator.init_workspace()
        generator.generate_schema()
        generator.deploy()

    print("\n[SUCCESS] All APIs processed successfully.")


if __name__ == "__main__":
    main()
