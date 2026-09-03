import re

def fix_newlines(html):
    return re.sub(r'\n(?![^<]*>)', '<br>', html)

print(fix_newlines("A <a href=\"#\"> B \n C </a>"))
