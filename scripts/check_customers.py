import requests
import urllib3

urllib3.disable_warnings()

# Login first (Flask uses session auth, not Basic auth)
session = requests.Session()
session.verify = False

login = session.post('https://127.0.0.1:5000/api/login',
                     json={'username': 'admin', 'password': 'admin123'})

if not login.ok:
    print(f'Login failed: {login.status_code} - {login.text}')
    exit(1)

# Now get customers
r = session.get('https://127.0.0.1:5000/api/customers')

if r.ok:
    customers = r.json()
    print(f'Total customers: {len(customers)}')
    for c in customers:
        print(f'  ID={c["id"]}, number={c.get("customer_number")}, name={c.get("name")}, auto={c.get("auto_enrolled")}')
else:
    print(f'Error: {r.status_code} - {r.text}')
