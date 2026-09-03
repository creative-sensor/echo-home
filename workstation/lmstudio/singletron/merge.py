#!/usr/bin/env python3
import ast
import argparse
import os
import glob

def topological_sort_definitions(def_order, definitions):
    """Sorts definitions to ensure base classes precede their derived classes."""
    dependencies = {name: [] for name in def_order}
    
    for name, node in definitions.items():
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id in definitions:
                    dependencies[name].append(base.id)
                    
    sorted_defs = []
    visited = set()
    
    def visit(n):
        if n in visited:
            return
        for dep in dependencies.get(n, []):
            visit(dep)
        visited.add(n)
        sorted_defs.append(n)
        
    for name in def_order:
        visit(name)
        
    return sorted_defs

def categorize_node(node, imports, definitions, def_order, global_stmts, main_stmts):
    """Sorts AST nodes into imports, definitions, globals, or main execution."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        imports.append(node)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        definitions[node.name] = node
        if node.name not in def_order:
            def_order.append(node.name)
    elif isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and \
         isinstance(node.test.left, ast.Name) and getattr(node.test.left, 'id', '') == '__name__':
        # Extract the body of any existing if __name__ == '__main__': block
        main_stmts.extend(node.body)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        # Variable and type-hinted assignments remain globals
        global_stmts.append(node)
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        # Preserve module docstrings or standalone constants as globals
        global_stmts.append(node)
    elif isinstance(node, (ast.Expr, ast.For, ast.While, ast.If, ast.With, ast.Try, ast.Assert, ast.Pass)):
        # Active procedural logic routes to main execution block
        main_stmts.append(node)
    else:
        global_stmts.append(node)

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

    with open(script_path, 'r', encoding='utf-8') as f:
        original_source = f.read()

    try:
        original_tree = ast.parse(original_source)
    except SyntaxError as e:
        print(f"[!] Syntax error in original script: {e}")
        return

    imports = []
    global_stmts = []
    definitions = {}
    def_order = []  
    main_stmts = []

    # 1. Parse original script
    for node in original_tree.body:
        categorize_node(node, imports, definitions, def_order, global_stmts, main_stmts)

    # 2. Override with the updated Module file (if it exists)
    module_file = os.path.join(meta_dir, f"Module.{module_name}")
    if os.path.exists(module_file):
        with open(module_file, 'r', encoding='utf-8') as f:
            module_source = f.read()
        try:
            module_tree = ast.parse(module_source)
            imports.clear()
            global_stmts.clear()
            main_stmts.clear()
            
            for node in module_tree.body:
                categorize_node(node, imports, definitions, def_order, global_stmts, main_stmts)
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
                categorize_node(node, imports, definitions, def_order, global_stmts, main_stmts)
        except SyntaxError as e:
            print(f"[!] Syntax error in component {filename}: {e}")

    unique_imports = {}
    for imp in imports:
        imp_str = ast.unparse(imp)
        if imp_str not in unique_imports:
            unique_imports[imp_str] = imp

    # Sort classes topologically based on inheritance
    def_order = topological_sort_definitions(def_order, definitions)

    # 4. Reconstruct the script
    final_code = "#!/usr/bin/env python\n\n"
    final_code += "# ---- GLOBAL ----\n"
    if unique_imports:
        final_code += "\n".join(ast.unparse(imp) for imp in unique_imports.values()) + "\n"
    if global_stmts:
        final_code += "\n" + "\n".join(ast.unparse(stmt) for stmt in global_stmts) + "\n"
    final_code += "# ---- GLOBAL.end ----\n\n"
    
    final_code += "# ---- DEFINITIONS (Classes & Functions) ----\n"
    for name in def_order:
        if name in definitions:
            final_code += ast.unparse(definitions[name]) + "\n\n"
    final_code += "# ---- DEFINITIONS.end ----\n\n"
    
    final_code += "# ---- MAIN ---\n"
    if main_stmts:
        # Dynamically construct the if __name__ == '__main__': block
        main_if = ast.If(
            test=ast.Compare(
                left=ast.Name(id='__name__', ctx=ast.Load()),
                ops=[ast.Eq()],
                comparators=[ast.Constant(value='__main__')]
            ),
            body=main_stmts,
            orelse=[]
        )
        final_code += ast.unparse(main_if) + "\n"
    final_code += "# ---- MAIN.end ----\n"

    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(final_code)
        
    print(f"[*] Successfully merged and standardized {script_name} via AST components.")

if __name__ == "__main__":
    main()
