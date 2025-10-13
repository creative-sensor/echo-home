#!/usr/bin/env python

import yaml,json,sys

def merge_yaml_keep_left(left_file, right_file):
    try:
        with open(left_file, 'r', encoding='utf-8') as f:
            left_data = yaml.safe_load(f) or {}
        with open(right_file, 'r', encoding='utf-8') as f:
            right_data = yaml.safe_load(f) or {}

        def update_dict(target, source):
            for key, value in source.items():
                if key not in target:
                    target[key] = value
                else:
                    for item in value["pfi"]:
                        if item not in target[key]["pfi"]:
                            target[key]["pfi"].append(item)

        update_dict(left_data, right_data)

        with open(left_file, 'w', encoding='utf-8') as f:
            for key, value in left_data.items():
                inline_json = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
                f.write(key + ": " + inline_json + "\n")
            

        print(f"Successfully merged '{left_file}' and '{right_file}' back into '{left_file}', keeping left file and update new from right.")

    except FileNotFoundError:
        print("Error: One or both input files not found.")
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}")



left_path = sys.argv[1]
right_path = sys.argv[2]
merge_yaml_keep_left(left_path, right_path)

