#!/usr/bin/env python
import argparse
import ast
import os
import json

def get_args(node):
    """Extracts argument names from a FunctionDef node."""
    args = []
    # Positional-only args (Python 3.8+)
    if getattr(node.args, 'posonlyargs', None):
        args.extend(a.arg for a in node.args.posonlyargs)
    # Standard args
    args.extend(a.arg for a in node.args.args)
    # *args (Removed the '*' prefix to prevent YAML alias parsing errors)
    if getattr(node.args, 'vararg', None):
        args.append(node.args.vararg.arg)
    # Keyword-only args
    if getattr(node.args, 'kwonlyargs', None):
        args.extend(a.arg for a in node.args.kwonlyargs)
    # **kwargs (Removed the '**' prefix to prevent YAML alias parsing errors)
    if getattr(node.args, 'kwarg', None):
        args.append(node.args.kwarg.arg)
    return args

def format_function(node, indent_level):
    """Formats a function definition matching the requested YAML-like structure."""
    ind = "  " * indent_level
    # Safely dump list to JSON/YAML compatible array
    args_list = json.dumps(get_args(node))
    ret = ast.unparse(node.returns) if getattr(node, 'returns', None) else "None"
    
    # Extract and unparse the body statements
    body_text = "\n".join(ast.unparse(stmt) for stmt in node.body)
    # Indent the body lines properly
    indented_body = "\n".join(f"{ind}    {line}" for line in body_text.splitlines())
    
    res = f"{ind}{node.name}:\n"
    res += f"{ind}  lineno: {node.lineno}\n"
    res += f"{ind}  end_lineno: {node.end_lineno}\n"
    res += f"{ind}  args: {args_list}\n"
    res += f"{ind}  return: {ret}\n"
    res += f"{ind}  body: |\n{indented_body}"
    
    return res

def format_class(node, indent_level):
    """Formats a class definition matching the requested YAML-like structure."""
    ind = "  " * indent_level
    
    # Extract and unparse the body statements
    body_text = "\n".join(ast.unparse(stmt) for stmt in node.body)
    indented_body = "\n".join(f"{ind}    {line}" for line in body_text.splitlines())
    
    res = f"{ind}{node.name}:\n"
    res += f"{ind}  lineno: {node.lineno}\n"
    res += f"{ind}  end_lineno: {node.end_lineno}\n"
    res += f"{ind}  body: |\n{indented_body}"
    
    return res

class DataflowVisitor(ast.NodeVisitor):
    """Traverses the AST to extract data flow, I/O alignment, and track external vs internal calls."""
    def __init__(self):
        self.components = {
            'MODULE': {
                'in': [],
                'out': 'None',
                'calls': [],
                'is_method': False,
                'method_name': None
            }
        }
        # Start at the module level context
        self.current_context = 'MODULE'
        self.current_class = None
        self.external_calls = set()

    def visit_ClassDef(self, node):
        prev_class = self.current_class
        self.current_class = node.name
        self.generic_visit(node)
        self.current_class = prev_class

    def visit_FunctionDef(self, node):
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._handle_function(node)

    def _handle_function(self, node):
        comp_name = f"{self.current_class}.{node.name}" if self.current_class else node.name
        in_args = get_args(node)
        out_type = ast.unparse(node.returns) if getattr(node, 'returns', None) else "None"

        self.components[comp_name] = {
            'in': in_args,
            'out': out_type,
            'calls': [],
            'is_method': self.current_class is not None,
            'method_name': node.name
        }

        prev_context = self.current_context
        self.current_context = comp_name
        self.generic_visit(node)
        self.current_context = prev_context

    def visit_Call(self, node):
        called_name = None
        is_internal = False

        if isinstance(node.func, ast.Name):
            called_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            if isinstance(node.func.value, ast.Name):
                # Correctly resolve internal self.<method> calls to ClassName.method_name
                if node.func.value.id == 'self' and self.current_class:
                    is_internal = True
                    called_name = f"{self.current_class}.{attr_name}"
                else:
                    called_name = f"{node.func.value.id}.{attr_name}"
            else:
                # Handles chained calls like MyClass().run()
                called_name = attr_name

        if called_name:
            if self.current_context and called_name not in self.components[self.current_context]['calls']:
                self.components[self.current_context]['calls'].append(called_name)
            
            # If not explicitly called on `self`, track it as an external dependency
            if not is_internal:
                self.external_calls.add(called_name)
                # Ensure the root method name is registered for cross-referencing
                if '.' in called_name:
                    self.external_calls.add(called_name.split('.')[-1])

        self.generic_visit(node)


def main():
    parser = argparse.ArgumentParser(description="Extract AST into a YAML-like structure.")
    parser.add_argument("-f", "--file", required=True, help="Path to the python script")
    args = parser.parse_args()

    file_path = args.file
    
    if not os.path.exists(file_path):
        print(f"Error: File '{file_path}' not found.")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)

    # Categorize root-level nodes
    functions = []
    classes = []
    top_level_stmts = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node)
        elif isinstance(node, ast.ClassDef):
            classes.append(node)
        else:
            top_level_stmts.append(node)
            
    # Map out the dataflow architecture
    visitor = DataflowVisitor()
    visitor.visit(tree)
    
    # Create a set of all internally defined components to filter external library calls
    defined_names = {f.name for f in functions} | {c.name for c in classes} | set(visitor.components.keys())

    # Map method names back to Class.method to resolve instance method calls (e.g. obj.method_name -> Class.method_name)
    defined_methods = {}
    for name in defined_names:
        if '.' in name:
            cls, mth = name.split('.', 1)
            defined_methods[mth] = name

    def get_valid_calls(raw_calls):
        valid = []
        for call in raw_calls:
            if call in defined_names:
                valid.append(call)
            elif '.' in call:
                # Resolve instance calls like `my_instance.run` -> `MyClass.run`
                _, mth = call.split('.', 1)
                if mth in defined_methods:
                    valid.append(defined_methods[mth])
            elif call in defined_methods:
                # Resolve chained calls like `MyClass().run()` where AST isolated `run`
                valid.append(defined_methods[call])
        
        # Deduplicate while preserving order
        return list(dict.fromkeys(valid))

    # Calculate absolute path to ensure accurate directory placement
    abs_file_path = os.path.abspath(file_path)
    base_dir = os.path.dirname(abs_file_path)
    filename = os.path.basename(abs_file_path)
    
    # Init meta folder in the same path as the script
    meta_dir_name = f".{filename}"
    meta_dir_path = os.path.join(base_dir, meta_dir_name)
    os.makedirs(meta_dir_path, exist_ok=True)
    
    output_yaml = os.path.join(meta_dir_path, "ast.yaml")
    module_name = os.path.splitext(filename)[0]

    # Write the YAML structure to the file
    with open(output_yaml, "w", encoding="utf-8") as out:
        def w(text=""):
            out.write(f"{text}\n")

        # 1. Print Standalone Functions
        if functions:
            w("FunctionDef:")
            for func in functions:
                w(format_function(func, 1))
            w()

        # 2. Print Classes 
        if classes:
            w("ClassDef:")
            for cls in classes:
                w(format_class(cls, 1))
            w()

        # 3. Print Main Module Segment
        w("Module:")
        w(f"  name: {module_name}")
        w("  body: |")
        if top_level_stmts:
            body_text = "\n".join(ast.unparse(stmt) for stmt in top_level_stmts)
            for line in body_text.splitlines():
                if line.strip():
                    w(f"    {line}")
                else:
                    w("")
        else:
            w("    # No top-level statements found outside of definitions.")
        w()

        # 4. Print Architecture Segment
        w("Architecture:")
        w("  definition:")
        
        if functions:
            w("    functions:")
            for func in functions:
                w(f"      - {func.name}: <DESCRIPTION>")

        if classes:
            w("    class:")
            for cls in classes:
                w(f"      - {cls.name}: <CLASS_DESCRIPTION>")
                
        w("  workflow: |")
        w("    <MODULE_DESCRIPTION>")
        
        # 5. Print Dataflow Graph
        w("  dataflow:")
        
        # Output MODULE explicitly first
        module_data = visitor.components.get('MODULE', {})
        if module_data:
            w("    MODULE:")
            valid_calls = get_valid_calls(module_data.get('calls', []))
            if valid_calls:
                w("      calls:")
                for call in valid_calls:
                    w(f"        - {call}")
            else:
                w("      calls: []")
        
        # Output the remaining functions/methods
        for comp_name, data in visitor.components.items():
            if comp_name == 'MODULE':
                continue
                
            # Exclude class methods that are unused or only called internally
            if data['is_method'] and data['method_name'] not in visitor.external_calls:
                continue
                
            w(f"    {comp_name}:")
            
            # --- UPDATED: Use json.dumps to safely quote arrays and escape characters ---
            in_str = json.dumps(data['in'])
            w(f"      in: {in_str}")
            w(f"      out: {data['out']}")
            
            valid_calls = get_valid_calls(data.get('calls', []))
            
            if valid_calls:
                w("      calls:")
                for call in valid_calls:
                    w(f"        - {call}")
            else:
                w("      calls: []")

    print(f"[*] AST successfully extracted and saved to: {output_yaml}")

if __name__ == "__main__":
    main()
