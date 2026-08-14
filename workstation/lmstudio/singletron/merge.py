#!/usr/bin/env python3
import ast
import argparse
import os
import glob

def main():
    parser = argparse.ArgumentParser(description="Restructure and merge Python script components.")
    parser.add_argument("script", help="Path to the original python script")
    args = parser.parse_args()

    script_path = args.script
    if not os.path.exists(script_path):
        print(f"[!] Target script {script_path} not found.")
        return

    base_dir = os.path.dirname(os.path.abspath(script_path))
    script_name = os.path.basename(script_path)
    module_name = os.path.splitext(script_name)[0]
    meta_dir = os.path.join(base_dir, f".{script_name}")

    if not os.path.exists(meta_dir):
        print(f"[!] Meta directory {meta_dir} not found.")
        return

    # 1. Parse the original script to categorize unchanged base components
    with open(script_path, 'r', encoding='utf-8') as f:
        original_source = f.read()

    try:
        original_tree = ast.parse(original_source)
    except SyntaxError as e:
        print(f"[!] Syntax error in original script: {e}")
        return

    imports = []
    functions = {}
    classes = {}
    main_stmts = []

    for node in original_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = node
        else:
            main_stmts.append(node)

    # 2. Override with the updated Module file (if it exists)
    module_file = os.path.join(meta_dir, f"Module.{module_name}")
    if os.path.exists(module_file):
        with open(module_file, 'r', encoding='utf-8') as f:
            module_source = f.read()
        try:
            module_tree = ast.parse(module_source)
            
            # Reset old top-level imports and main statements
            imports = []
            main_stmts = []
            
            for node in module_tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports.append(node)
                # Ignore functions/classes accidentally injected into module scope
                elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    main_stmts.append(node)
        except SyntaxError as e:
            print(f"[!] Syntax error in updated Module file: {e}")

    # 3. Override with updated Functions and Classes
    for comp_file in glob.glob(os.path.join(meta_dir, "*.*")):
        filename = os.path.basename(comp_file)
        if filename.startswith("Module.") or filename.endswith(".yaml"):
            continue
            
        with open(comp_file, 'r', encoding='utf-8') as f:
            comp_source = f.read()
            
        try:
            comp_tree = ast.parse(comp_source)
            for node in comp_tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[node.name] = node
                elif isinstance(node, ast.ClassDef):
                    classes[node.name] = node
        except SyntaxError as e:
            print(f"[!] Syntax error in component {filename}: {e}")

    # 4. Reconstruct the strict standardized format
    final_code = "# ---- GLOBAL ----\n"
    if imports:
        final_code += ast.unparse(imports) + "\n"
    final_code += "# ---- GLOBAL.end ----\n"
    
    final_code += "# ---- FUNCTION ----\n"
    if functions:
        final_code += ast.unparse(list(functions.values())) + "\n"
    final_code += "# ---- FUNCTION.end ----\n"
    
    final_code += "# ---- CLASS ----\n"
    if classes:
        final_code += ast.unparse(list(classes.values())) + "\n"
    final_code += "# ---- CLASS.end ----\n"
    
    final_code += "# ---- MAIN ---\n"
    if main_stmts:
        final_code += ast.unparse(main_stmts) + "\n"
    final_code += "# ---- MAIN.end ----\n"

    # 5. Overwrite the original script
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(final_code)
        
    print(f"[*] Successfully merged and standardized {script_name} via AST components.")

if __name__ == "__main__":
    main()
