import subprocess
import requests
import re
session = requests.Session()

# Get login CSRF
res = session.get("http://localhost:8000/portal/login/")
csrf_token = session.cookies.get("csrftoken")
if not csrf_token:
    print("NO CSRF TOKEN IN COOKIE")

# Assuming we can't easily login without a password, let's just create a superuser and login.
