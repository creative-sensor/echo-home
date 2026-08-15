#!/usr/bin/env python
import argparse
import ast
import os

def get_args(node):
    """Extracts argument names from a FunctionDef node."""
    args = []
    # Positional-only args (Python 3.8+)
    if getattr(node.args, 'posonlyargs', None):
        args.extend(a.arg for a in node.args.posonlyargs)
    # Standard args
    args.extend(a.arg for a in node.args.args)
    # *args
    if getattr(node.args, 'vararg', None):
        args.append(f"*{node.args.vararg.arg}")
    # Keyword-only args
    if getattr(node.args, 'kwonlyargs', None):
        args.extend(a.arg for a in node.args.kwonlyargs)
    # **kwargs
    if getattr(node.args, 'kwarg', None):
        args.append(f"**{node.args.kwarg.arg}")
    return args

def format_function(node, indent_level):
    """Formats a function definition matching the requested YAML-like structure."""
    ind = "  " * indent_level
    args_list = "['" + "', '".join(get_args(node))+"']" if get_args(node) else "[]"
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

    print(f"[*] AST successfully extracted and saved to: {output_yaml}")

if __name__ == "__main__":
    main()
