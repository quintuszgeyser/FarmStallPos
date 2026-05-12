import requests
import urllib3

urllib3.disable_warnings()

r = requests.get('https://127.0.0.1:5000/api/customers',
                 auth=('admin', 'admin123'),
                 verify=False)

if r.ok:
    customers = r.json()
    print(f'Total customers: {len(customers)}')
    for c in customers:
        print(f'  ID={c["id"]}, number={c.get("customer_number")}, name={c.get("name")}, auto={c.get("auto_enrolled")}')
else:
    print(f'Error: {r.status_code} - {r.text}')
