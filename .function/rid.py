#!/usr/bin/env python3
import sys
import uuid
import secrets
import string

def generate_random_string(length):
    """Generates a random alphanumeric string of a specified length."""
    if length <= 0:
        return ""
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for i in range(length))

def main():
    # Check for command-line argument
    if len(sys.argv) > 1:
        try:
            length = int(sys.argv[1])
        except ValueError:
            print("Error: Length argument must be an integer.", file=sys.stderr)
            sys.exit(1)
    else:
        # Default length if no argument is provided
        length = 4

    # Generate RID
    rid = generate_random_string(length)
    
    # Generate UUID
    uuid_str = str(uuid.uuid4())
    
    # Generate lowercase RID (by converting the generated string)
    rid_no_case = rid.lower()

    print(f"uuid  {uuid_str}")
    print(f"rid-{rid:<{length}}  {rid}")
    print(f"rid-{rid_no_case:<{length}}  {rid_no_case}")

if __name__ == "__main__":
    main()
