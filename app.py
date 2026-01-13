
from flask import Flask, render_template, jsonify, request
from datetime import datetime
import json, os

TRANSACTIONS_FILE = 'transactions.json'
PRODUCTS_FILE = 'products.json'
CURRENCY = os.getenv('CURRENCY', 'R')  # Default to South African Rand; override with env var

app = Flask(__name__)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True

# --- Helpers ---
def load_transactions():
    if os.path.exists(TRANSACTIONS_FILE):
        with open(TRANSACTIONS_FILE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def save_transactions(transactions):
    with open(TRANSACTIONS_FILE, 'w') as f:
        json.dump(transactions, f, indent=2)


def load_products():
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_products(products):
    with open(PRODUCTS_FILE, 'w') as f:
        json.dump(products, f, indent=2)


# --- Routes ---
@app.get('/')
def index():
    return render_template('index.html', currency=CURRENCY)


@app.get('/api/products')
def api_get_products():
    return jsonify(load_products())


@app.post('/api/products')
def api_add_product():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    price = float(data.get('price', 0))
    if not name:
        return jsonify({'error': 'Product name required'}), 400
    products = load_products()
    if name in products:
        return jsonify({'error': 'Product already exists'}), 409
    # assign next id
    next_id = 1
    if products:
        next_id = max(v.get('id', 0) for v in products.values()) + 1
    products[name] = {'id': next_id, 'price': price}
    save_products(products)
    return jsonify({'ok': True, 'products': products})


@app.post('/api/products/update')
def api_update_product():
    data = request.get_json(force=True)
    old_name = (data.get('old_name') or '').strip()
    new_name = (data.get('new_name') or '').strip()
    price = float(data.get('price', 0))
    products = load_products()
    if old_name not in products:
        return jsonify({'error': 'Original product not found'}), 404
    prod_id = products[old_name]['id']
    # If renaming, remove old entry
    if new_name and new_name != old_name:
        del products[old_name]
    target_name = new_name or old_name
    products[target_name] = {'id': prod_id, 'price': price}
    save_products(products)
    return jsonify({'ok': True, 'products': products})


@app.delete('/api/products/<name>')
def api_delete_product(name):
    products = load_products()
    if name not in products:
        return jsonify({'error': 'Product not found'}), 404
    del products[name]
    save_products(products)
    return jsonify({'ok': True, 'products': products})


@app.get('/api/transactions')
def api_get_transactions():
    return jsonify(load_transactions())


@app.post('/api/transactions')
def api_add_transaction():
    """
    Expected payload:
    {
      "items": [
        {"product_name": "Apple", "qty": 2}, ...
      ]
    }
    Server will expand into per-line records similar to the Tkinter app:
    {tran_id, date_time, product_id/name, no_of_items, amount}
    """
    data = request.get_json(force=True)
    items = data.get('items') or []
    if not items:
        return jsonify({'error': 'No items'}), 400

    products = load_products()
    transactions = load_transactions()
    # Next transaction ID
    next_tran_id = 1
    if transactions:
        next_tran_id = max(t.get('tran_id', 0) for t in transactions) + 1

    date_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    new_lines = []
    for it in items:
        name = (it.get('product_name') or '').strip()
        qty = int(it.get('qty', 1))
        # Look up by name or numeric id supplied as string
        price = None
        prod_id = None
        if name in products:
            price = float(products[name]['price'])
            prod_id = products[name]['id']
        else:
            # try match by id
            try:
                as_id = int(name)
                for n, v in products.items():
                    if v.get('id') == as_id:
                        price = float(v['price'])
                        prod_id = as_id
                        name = n
                        break
            except Exception:
                pass
        if price is None:
            return jsonify({'error': f'Product "{name}" not found'}), 404
        amount = price * qty
        new_lines.append({
            'tran_id': next_tran_id,
            'date_time': date_time,
            'product_id': name,  # keep compatible with original (name as id)
            'no_of_items': qty,
            'amount': amount
        })

    transactions.extend(new_lines)
    save_transactions(transactions)
    return jsonify({'ok': True, 'tran_id': next_tran_id, 'lines': new_lines})


if __name__ == '__main__':
    # Bind to all interfaces so phones on same Wi-Fi can connect
    app.run(host='0.0.0.0', port=5000, debug=True)
