import os
import re

files = [
    "hotaru/response.py",
    "hotaru/runtime.py",
    "relay/proxies.py",
    "relay/sandbox.py"
]

for filepath in files:
    with open(filepath, 'r') as f:
        content = f.read()
    
    # We want to replace <var>.replace("\n", "<br>") with __import__("re").sub(r'\n(?![^<]*>)', '<br>', <var>)
    # Using a regex:
    new_content = re.sub(
        r'([a-zA-Z0-9_]+(?:\(value\))?)\.replace\("\\n", "<br>"\)',
        r'__import__("re").sub(r"\\n(?![^<]*>)", "<br>", \1)',
        content
    )
    
    with open(filepath, 'w') as f:
        f.write(new_content)
        
    print(f"Fixed {filepath}")
