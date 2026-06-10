import requests
import urllib3

urllib3.disable_warnings()

# Login first
session = requests.Session()
session.verify = False

login = session.post('https://127.0.0.1:5000/api/login',
                     json={'username': 'admin', 'password': 'admin123'})

if not login.ok:
    print(f'Login failed: {login.status_code} - {login.text}')
    exit(1)

# Get customers
r = session.get('https://127.0.0.1:5000/api/customers')
if not r.ok:
    print(f'Error: {r.status_code} - {r.text}')
    exit(1)

customers = r.json()
print(f'Total customers: {len(customers)}')

# Check which customers have faces
customers_with_faces = []
for c in customers:
    if c.get('has_face'):
        customers_with_faces.append(c)

print(f'Customers with faces enrolled: {len(customers_with_faces)}')
for c in customers_with_faces:
    print(f'  ID={c["id"]}, number={c.get("customer_number")}, name={c.get("name")}, has_face={c.get("has_face")}, has_gait={c.get("has_gait")}')

# Check faces_raw endpoint
print('\nChecking /api/customers/faces_raw...')
r = session.get('https://127.0.0.1:5000/api/customers/faces_raw')
if not r.ok:
    print(f'Error: {r.status_code} - {r.text}')
else:
    faces = r.json()
    print(f'Total face embeddings in database: {len(faces)}')
    if len(faces) > 0:
        print(f'Sample: customer_id={faces[0].get("customer_id")}, embedding length={len(faces[0].get("embedding_b64", ""))} chars')
