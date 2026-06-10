"""
Test Flask connection from localhost to diagnose connection resets.
Run on Mini PC: .venv\Scripts\python.exe test_flask_connection.py
"""

import requests
import time

# Disable SSL warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def test_connection(url, method='GET'):
    """Test a single connection."""
    try:
        print(f"\nTesting {method} {url}")
        if method == 'GET':
            r = requests.get(url, timeout=5, verify=False)
        else:
            r = requests.post(url, json={'username': 'admin', 'password': 'admin123'}, timeout=5, verify=False)

        print(f"  ✓ Status: {r.status_code}")
        print(f"  ✓ Response: {r.text[:100]}")
        return True
    except requests.exceptions.ConnectionError as e:
        print(f"  ✗ ConnectionError: {e}")
        return False
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return False

if __name__ == '__main__':
    print("=" * 80)
    print("Flask Connection Diagnostic")
    print("=" * 80)

    # Test various URLs
    urls = [
        ('http://127.0.0.1:5000/', 'GET'),
        ('http://127.0.0.1:5000/api/me', 'GET'),
        ('http://127.0.0.1:5000/api/login', 'POST'),
        ('http://localhost:5000/', 'GET'),
        ('http://localhost:5000/api/me', 'GET'),
    ]

    results = []
    for url, method in urls:
        success = test_connection(url, method)
        results.append((url, method, success))
        time.sleep(1)

    print("\n" + "=" * 80)
    print("Summary:")
    print("=" * 80)
    for url, method, success in results:
        status = "✓" if success else "✗"
        print(f"  {status} {method} {url}")

    if not any(r[2] for r in results):
        print("\n❌ All connections failed. Flask may not be running or is rejecting connections.")
    else:
        print("\n✓ Some connections succeeded.")
