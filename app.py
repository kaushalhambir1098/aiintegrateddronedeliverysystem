"""
Smart Drone Delivery — Enhanced Single-file App
Features:
 - Manual pickup/hub selection (two clicks)
 - Create / Start / Mark Delivered / Delete orders
 - CSV Export & CSV Import (admin)
 - Multi-order tracking on main map (polling)
 - Multi-segment simulated route per order (stored as JSON)
 - Simple admin authentication + Admin dashboard
"""

from flask import (
    Flask, g, render_template_string, request, jsonify, redirect, url_for,
    session, send_file, flash
)
import sqlite3, os, math, time, json, io, csv, random
from datetime import timedelta
from werkzeug.utils import secure_filename

# ---------------- Configuration ----------------
APP_PORT = 5000
DB_PATH = 'orders.db'
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'csv'}
# Basic admin credentials (change before real use)
ADMIN_USER = 'admin'
ADMIN_PASS = 'password'   # change this in production

# Flask app
app = Flask(__name__)
app.secret_key = 'change_this_secret'  # change for production
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------------- Database helpers & schema migration ----------------
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        need_init = not os.path.exists(DB_PATH)
        db = g._database = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
        if need_init:
            init_db(db)
        else:
            # ensure migration: add route_json column if missing
            ensure_columns(db)
    return db

def init_db(db_conn):
    cur = db_conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT,
            pickup_lat REAL,
            pickup_lng REAL,
            hub_lat REAL,
            hub_lng REAL,
            distance_m REAL,
            speed_mps REAL,
            created_at REAL,
            start_time REAL,
            duration_s REAL,
            status TEXT,
            route_json TEXT
        )
    ''')
    db_conn.commit()

def ensure_columns(db_conn):
    # Add route_json column if not present
    cur = db_conn.cursor()
    cur.execute("PRAGMA table_info(orders)")
    cols = [r['name'] for r in cur.fetchall()]
    if 'route_json' not in cols:
        try:
            cur.execute("ALTER TABLE orders ADD COLUMN route_json TEXT")
            db_conn.commit()
        except Exception:
            pass

@app.teardown_appcontext
def close_connection(exc):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# ---------------- Utilities ----------------
def haversine_m(lat1, lon1, lat2, lon2):
    # returns meters
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    c = 2 * math.asin(min(1, math.sqrt(a)))
    R = 6371000
    return R * c

def generate_route_with_turns(lat1, lon1, lat2, lon2, points=12, amplitude=0.0008):
    """
    Generate a multi-segment route between two lat/lon coordinates.
    Adds small perpendicular sinusoidal offsets to create 'turns'.
    Returns list of [lat, lon] points.
    amplitude controls how big the lateral offsets are.
    """
    pts = []
    for i in range(points+1):
        t = i/points
        lat = lat1 + (lat2 - lat1) * t
        lon = lon1 + (lon2 - lon1) * t
        # perpendicular offset direction
        dx = lon2 - lon1
        dy = lat2 - lat1
        # normalized perpendicular
        norm = math.hypot(dx, dy) or 1.0
        px = -dy / norm
        py = dx / norm
        # sinusoidal offset along the path (no offset at start/end)
        offset = math.sin(math.pi * t * 2) * (1 - abs(t*2-1)) * amplitude
        lat += py * offset
        lon += px * offset
        # add slight randomness to avoid perfectly symmetric patterns
        lat += (random.random()-0.5) * amplitude*0.15
        lon += (random.random()-0.5) * amplitude*0.15
        pts.append([lat, lon])
    return pts

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------------- Authentication helpers ----------------
def is_logged_in():
    return session.get('logged_in') is True

def require_admin(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_logged_in():
            return redirect(url_for('admin_login', next=request.path))
        return fn(*a, **kw)
    return wrapper

# ---------------- Templates (kept compact to a single file) ----------------
# We'll reuse the earlier interface; index shows the main map & order table,
# admin page allows CSV import/export, login/logout.
INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Smart Drone Delivery — Enhanced</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    body { padding: 12px; }
    #map { height: 520px; border: 1px solid #ddd; }
    .controls { margin-bottom: 8px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    input[type=text], input[type=number] { width: 180px; padding:6px; }
    table { font-size: .9rem; }
  </style>
</head>
<body>
  <div class="container-fluid">
    <div class="d-flex justify-content-between align-items-start mb-2">
      <div>
        <h3>Smart Drone Delivery — Enhanced</h3>
        <p class="text-muted mb-0">Click <strong>New Order</strong> then click <strong>pickup</strong> and then <strong>hub</strong>. Fill package name and speed then Create Order. Orders appear below and on map.</p>
      </div>
      <div class="text-end">
        {% if session.logged_in %}
          <a href="{{ url_for('admin_dashboard') }}" class="btn btn-outline-secondary btn-sm">Admin</a>
          <a href="{{ url_for('admin_logout') }}" class="btn btn-outline-danger btn-sm">Logout</a>
        {% else %}
          <a href="{{ url_for('admin_login') }}" class="btn btn-outline-primary btn-sm">Admin Login</a>
        {% endif %}
      </div>
    </div>

    <div class="controls">
      <button id="newOrderBtn" class="btn btn-primary btn-sm">New Order</button>
      <label class="mb-0">Package name
        <input id="packageName" type="text" value="Package 1" class="form-control form-control-sm ms-2" />
      </label>
      <label class="mb-0">Speed (m/s)
        <input id="speed" type="number" value="8" step="0.1" class="form-control form-control-sm ms-2" />
      </label>
      <button id="createBtn" class="btn btn-success btn-sm">Create Order</button>
      <button id="clearBtn" class="btn btn-secondary btn-sm">Clear Map</button>
      <div class="ms-auto">
        <small class="text-muted">Multi-order tracking & multi-segment routes enabled</small>
      </div>
    </div>

    <div id="map"></div>

    <h5 class="mt-3">Orders</h5>
    <div class="table-responsive">
      <table class="table table-sm table-bordered">
        <thead class="table-light"><tr><th>ID</th><th>Package</th><th>From → To</th><th>Distance (m)</th><th>ETA (s)</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>
          {% for o in orders %}
            <tr>
              <td>{{o.id}}</td>
              <td>{{o.package_name}}</td>
              <td>{{'%.4f'|format(o.pickup_lat)}},{{'%.4f'|format(o.pickup_lng)}} → {{'%.4f'|format(o.hub_lat)}},{{'%.4f'|format(o.hub_lng)}}</td>
              <td>{{'%.1f'|format(o.distance_m)}}</td>
              <td>{% if o.duration_s %}{{'%.1f'|format(o.duration_s)}}{% else %}-{% endif %}</td>
              <td>{{o.status}}</td>
              <td>
                <a href="{{ url_for('track', order_id=o.id) }}" class="btn btn-sm btn-outline-primary">Track</a>
                {% if o.status == 'Pending' %}
                  <a href="{{ url_for('start_order', order_id=o.id) }}" class="btn btn-sm btn-success">Start</a>
                {% endif %}
                <button class="btn btn-sm btn-danger" onclick="deleteOrder({{o.id}})">Delete</button>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  // Map init
  const map = L.map('map').setView([20.5937,78.9629], 5);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(map);

  // Icons
  const greenIcon = L.icon({ iconUrl: 'https://maps.google.com/mapfiles/ms/icons/green-dot.png', iconSize:[32,32], iconAnchor:[16,32] });
  const redIcon = L.icon({ iconUrl: 'https://maps.google.com/mapfiles/ms/icons/red-dot.png', iconSize:[32,32], iconAnchor:[16,32] });
  const pkgIcon = L.icon({ iconUrl: 'https://cdn-icons-png.flaticon.com/512/685/685352.png', iconSize:[30,30], iconAnchor:[15,15] });

  // State
  let modeNewOrder = false;
  let clickStep = 0;
  let pickupMarker = null;
  let hubMarker = null;
  let routeLine = null;
  let current = null; // {pickup_lat,pickup_lng,hub_lat,hub_lng}

  // For multi-order tracking
  const orderMarkers = {};   // order_id => marker
  const orderPolylines = {}; // order_id => polyline

  document.getElementById('newOrderBtn').addEventListener('click', ()=>{
    modeNewOrder = true;
    clickStep = 1;
    alert('New Order: click map once to set PICKUP, then click again for HUB.');
  });
  document.getElementById('clearBtn').addEventListener('click', clearMap);

  function clearMap(){
    modeNewOrder = false;
    clickStep = 0;
    current = null;
    if(pickupMarker){ map.removeLayer(pickupMarker); pickupMarker = null; }
    if(hubMarker){ map.removeLayer(hubMarker); hubMarker = null; }
    if(routeLine){ map.removeLayer(routeLine); routeLine = null; }
  }

  map.on('click', function(e){
    if(!modeNewOrder) return;
    if(clickStep === 1){
      if(pickupMarker) map.removeLayer(pickupMarker);
      pickupMarker = L.marker(e.latlng, {icon: greenIcon}).addTo(map).bindPopup('Pickup').openPopup();
      clickStep = 2;
      alert('Pickup set. Now click map to set HUB.');
    } else if(clickStep === 2){
      if(hubMarker) map.removeLayer(hubMarker);
      hubMarker = L.marker(e.latlng, {icon: redIcon}).addTo(map).bindPopup('Hub').openPopup();
      if(routeLine) map.removeLayer(routeLine);
      routeLine = L.polyline([pickupMarker.getLatLng(), hubMarker.getLatLng()], {color:'blue'}).addTo(map);
      current = {
        pickup_lat: pickupMarker.getLatLng().lat,
        pickup_lng: pickupMarker.getLatLng().lng,
        hub_lat: hubMarker.getLatLng().lat,
        hub_lng: hubMarker.getLatLng().lng
      };
      modeNewOrder = false;
      clickStep = 0;
      alert('Pickup & Hub set. Enter package name/speed, then click Create Order.');
    }
  });

  document.getElementById('createBtn').addEventListener('click', async ()=>{
    if(!current) return alert('No pickup & hub set. Click New Order first.');
    const pkg = document.getElementById('packageName').value || 'Package 1';
    const speed = parseFloat(document.getElementById('speed').value) || 8.0;
    const payload = Object.assign({package_name: pkg, speed_mps: speed}, current);
    const res = await fetch('/create_order', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const js = await res.json();
    if(js.error) { alert('Error: ' + js.error); return; }
    alert('Order created (distance: ' + Math.round(js.distance_m) + ' m, ETA: ' + Math.round(js.duration_s) + ' s)');
    clearMap();
    // draw new order marker & polyline immediately
    await fetchAndRenderOrders();
  });

  async function deleteOrder(id){
    if(!confirm('Delete order ' + id + '?')) return;
    await fetch('/delete_order/' + id, { method: 'POST' });
    await fetchAndRenderOrders();
  }

  // Fetch orders status for all orders (includes current_lat/current_lng)
  async function fetchOrders() {
    const res = await fetch('/api/orders');
    return await res.json();
  }

  // Update or create markers/polylines for each order
  async function fetchAndRenderOrders(){
    const data = await fetchOrders();
    const orders = data.orders;
    // remove markers/polylines that are not present anymore
    const presentIds = new Set(orders.map(o=>o.id));
    for(const id in orderMarkers){
      if(!presentIds.has(parseInt(id))){
        try{ map.removeLayer(orderMarkers[id]); }catch(e){}
        delete orderMarkers[id];
      }
    }
    for(const id in orderPolylines){
      if(!presentIds.has(parseInt(id))){
        try{ map.removeLayer(orderPolylines[id]); }catch(e){}
        delete orderPolylines[id];
      }
    }
    for(const o of orders){
      // draw polyline if not exists
      if(o.route && !orderPolylines[o.id]){
        const pts = o.route;
        orderPolylines[o.id] = L.polyline(pts, {color:'#888', weight:2, dashArray:'6,6'}).addTo(map);
      }
      // marker
      const lat = o.current_lat || o.pickup_lat;
      const lng = o.current_lng || o.pickup_lng;
      if(!orderMarkers[o.id]){
        const m = L.marker([lat, lng], { icon: pkgIcon, title: o.package_name }).addTo(map);
        m.bindTooltip(o.package_name + ' (' + o.status + ')', {permanent:false});
        orderMarkers[o.id] = m;
      } else {
        orderMarkers[o.id].setLatLng([lat, lng]);
        // update tooltip
        orderMarkers[o.id].setTooltipContent(o.package_name + ' (' + o.status + ')');
      }
    }
  }

  // Polling loop: update every 1.5s
  fetchAndRenderOrders();
  setInterval(fetchAndRenderOrders, 1500);

  // center map on selected polyline (optional): clicking table track links handled by separate page

</script>
</body>
</html>
"""

ADMIN_LOGIN_HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Admin Login</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>body{padding:20px}</style>
</head>
<body>
  <div class="container" style="max-width:420px;margin-top:40px">
    <h4>Admin Login</h4>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="alert alert-danger">{{ messages[0] }}</div>
      {% endif %}
    {% endwith %}
    <form method="post">
      <div class="mb-2"><label>Username</label><input name="username" class="form-control" required></div>
      <div class="mb-2"><label>Password</label><input name="password" type="password" class="form-control" required></div>
      <button class="btn btn-primary">Login</button>
      <a href="{{ url_for('index') }}" class="btn btn-link">Back</a>
    </form>
  </div>
</body>
</html>
"""

ADMIN_DASH_HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Admin Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<style>body{padding:16px}</style>
</head>
<body>
  <div class="container">
    <h4>Admin Dashboard</h4>
    <p>CSV export/import for orders</p>

    <div class="mb-3">
      <a href="{{ url_for('export_csv') }}" class="btn btn-outline-success">Export Orders CSV</a>
    </div>

    <div class="mb-3">
      <form method="post" enctype="multipart/form-data" action="{{ url_for('import_csv') }}">
        <div class="mb-2">
          <label class="form-label">Import CSV (columns: package_name,pickup_lat,pickup_lng,hub_lat,hub_lng,speed_mps)</label>
          <input type="file" name="file" class="form-control" accept=".csv" required>
        </div>
        <button class="btn btn-primary">Upload & Import</button>
      </form>
    </div>

    <div class="mb-3">
      <a href="{{ url_for('index') }}" class="btn btn-link">Back to app</a>
      <a href="{{ url_for('admin_logout') }}" class="btn btn-danger">Logout</a>
    </div>

    {% if imported_count is defined %}
      <div class="alert alert-info">{{ imported_count }} rows imported.</div>
    {% endif %}
  </div>
</body>
</html>
"""

TRACK_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Track Order #{{order.id}}</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>body{padding:12px} #map{height:600px;border:1px solid #ddd}</style>
</head>
<body>
  <a href="/" class="btn btn-link">&larr; Back</a>
  <h4>Track Order #{{order.id}} — {{order.package_name}}</h4>
  <div>Status: <strong id="statusText">{{order.status}}</strong></div>
  <div class="mt-2" id="map"></div>
  <div class="mt-2">
    <button id="markDeliveredBtn" class="btn btn-success btn-sm">Mark Delivered</button>
  </div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const orderId = {{order.id}};
const route = {{ order.route_json if order.route_json else 'null' }};
const pickup = [{{order.pickup_lat}}, {{order.pickup_lng}}];
const hub = [{{order.hub_lat}}, {{order.hub_lng}}];

const map = L.map('map').setView(pickup, 14);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);

const pickupMarker = L.marker(pickup).addTo(map).bindPopup('Pickup');
const hubMarker = L.marker(hub).addTo(map).bindPopup('Hub');

let poly = null;
if(route){
  poly = L.polyline(route, {color:'#007bff', weight:3}).addTo(map);
} else {
  poly = L.polyline([pickup, hub], {color:'#007bff', weight:3}).addTo(map);
}

const pkgMarker = L.marker(pickup).addTo(map);
pkgMarker.bindTooltip("{{order.package_name}}", {permanent:true, direction:'top', offset:[0,-12]}).openTooltip();

function setStatusText(s){ document.getElementById('statusText').innerText = s; }

async function fetchStatus(){
  try {
    const res = await fetch('/api/order/' + orderId + '/status');
    const js = await res.json();
    setStatusText(js.status);
    if(js.current_lat !== undefined && js.current_lng !== undefined){
      pkgMarker.setLatLng([js.current_lat, js.current_lng]);
    }
    if(js.status === 'Delivered'){ clearInterval(window.pollHandle); }
  } catch(e){ console.error(e); }
}

fetchStatus();
window.pollHandle = setInterval(fetchStatus, 1000);

document.getElementById('markDeliveredBtn').addEventListener('click', async ()=>{
  await fetch('/update_status/' + orderId, { method: 'POST' });
  fetchStatus();
});
</script>
</body>
</html>
"""

# ---------------- Routes ----------------

@app.route('/')
def index():
    db = get_db()
    cur = db.execute('SELECT * FROM orders ORDER BY id DESC')
    orders = cur.fetchall()
    return render_template_string(INDEX_HTML, orders=orders)

@app.route('/create_order', methods=['POST'])
def create_order():
    payload = request.get_json()
    if not payload:
        return jsonify({'error': 'JSON expected'}), 400
    try:
        pkg = payload.get('package_name', 'Package 1')
        pickup_lat = float(payload['pickup_lat'])
        pickup_lng = float(payload['pickup_lng'])
        hub_lat = float(payload['hub_lat'])
        hub_lng = float(payload['hub_lng'])
        speed = float(payload.get('speed_mps') or payload.get('speed') or 8.0)
    except Exception as e:
        return jsonify({'error': f'Invalid payload: {e}'}), 400

    distance = haversine_m(pickup_lat, pickup_lng, hub_lat, hub_lng)
    duration = distance / speed if speed > 0 else None
    created_at = time.time()
    # generate multi-segment route points (lat,lng)
    route_pts = generate_route_with_turns(pickup_lat, pickup_lng, hub_lat, hub_lng, points=18)

    db = get_db()
    cur = db.cursor()
    cur.execute('''
       INSERT INTO orders (
         package_name,pickup_lat,pickup_lng,hub_lat,hub_lng,distance_m,speed_mps,created_at,start_time,duration_s,status,route_json
       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (pkg, pickup_lat, pickup_lng, hub_lat, hub_lng, distance, speed, created_at, None, duration, 'Pending', json.dumps(route_pts)))
    db.commit()
    oid = cur.lastrowid
    return jsonify({'message':'created','order_id':oid,'distance_m':distance,'duration_s':duration})

@app.route('/start/<int:order_id>')
def start_order(order_id):
    db = get_db()
    cur = db.execute('SELECT * FROM orders WHERE id=?', (order_id,))
    r = cur.fetchone()
    if not r:
        return "Order not found", 404
    if r['status'] != 'Pending':
        return redirect(url_for('track', order_id=order_id))
    start_time = time.time()
    db = get_db()
    db.execute('UPDATE orders SET start_time=?, status=? WHERE id=?', (start_time, 'In Transit', order_id))
    db.commit()
    return redirect(url_for('track', order_id=order_id))

@app.route('/track/<int:order_id>')
def track(order_id):
    db = get_db()
    cur = db.execute('SELECT * FROM orders WHERE id=?', (order_id,))
    r = cur.fetchone()
    if not r:
        return "Order not found", 404
    return render_template_string(TRACK_HTML, order=r)

@app.route('/api/order/<int:order_id>/status')
def api_order_status(order_id):
    db = get_db()
    cur = db.execute('SELECT * FROM orders WHERE id=?', (order_id,))
    r = cur.fetchone()
    if not r:
        return jsonify({'error':'not found'}), 404

    status = r['status']
    pickup_lat = r['pickup_lat']; pickup_lng = r['pickup_lng']
    hub_lat = r['hub_lat']; hub_lng = r['hub_lng']
    distance = r['distance_m'] or 0.0
    start_time = r['start_time']
    duration = r['duration_s'] or 0.0
    route = json.loads(r['route_json']) if r['route_json'] else None
    now = time.time()

    out = {
        'id': r['id'],
        'package_name': r['package_name'],
        'status': status,
        'distance_m': distance,
        'speed_mps': r['speed_mps'],
    }

    if status == 'Pending' or not start_time:
        out.update({'current_lat': pickup_lat, 'current_lng': pickup_lng, 'elapsed_seconds': 0, 'eta_seconds': None, 'route': route})
        return jsonify(out)

    elapsed = now - start_time
    frac = elapsed / duration if duration and duration>0 else 1.0
    if frac >= 1.0:
        # mark delivered
        db = get_db()
        db.execute('UPDATE orders SET status=?, start_time=? WHERE id=?', ('Delivered', start_time, order_id))
        db.commit()
        out.update({'status':'Delivered','current_lat': hub_lat, 'current_lng': hub_lng, 'elapsed_seconds': duration, 'eta_seconds': 0, 'route': route})
        return jsonify(out)
    else:
        # follow route points if available
        if route and isinstance(route, list) and len(route) > 1:
            # compute index along route
            idx = min(len(route)-1, max(0, int(frac * (len(route)-1))))
            c_lat, c_lng = route[idx][0], route[idx][1]
        else:
            # linear interp
            c_lat = pickup_lat + (hub_lat - pickup_lat) * frac
            c_lng = pickup_lng + (hub_lng - pickup_lng) * frac
        out.update({'current_lat': c_lat, 'current_lng': c_lng, 'elapsed_seconds': elapsed, 'eta_seconds': max(0, duration - elapsed), 'route': route})
        return jsonify(out)

@app.route('/api/orders')
def api_orders():
    # return all orders with their current positions (for multi-order tracking)
    db = get_db()
    cur = db.execute('SELECT * FROM orders')
    rows = cur.fetchall()
    out = []
    for r in rows:
        # reuse logic in api_order_status but inline for batch
        status = r['status']
        pickup_lat = r['pickup_lat']; pickup_lng = r['pickup_lng']
        hub_lat = r['hub_lat']; hub_lng = r['hub_lng']
        distance = r['distance_m'] or 0.0
        start_time = r['start_time']
        duration = r['duration_s'] or 0.0
        route = json.loads(r['route_json']) if r['route_json'] else None
        now = time.time()
        item = {'id': r['id'], 'package_name': r['package_name'], 'status': status, 'distance_m': distance, 'pickup_lat': pickup_lat, 'pickup_lng': pickup_lng, 'hub_lat': hub_lat, 'hub_lng': hub_lng, 'speed_mps': r['speed_mps'], 'route': route}
        if status == 'Pending' or not start_time:
            item.update({'current_lat': pickup_lat, 'current_lng': pickup_lng})
        else:
            elapsed = now - start_time
            frac = elapsed / duration if duration and duration>0 else 1.0
            if frac >= 1.0:
                item.update({'current_lat': hub_lat, 'current_lng': hub_lng, 'status': 'Delivered'})
            else:
                if route and isinstance(route, list) and len(route)>1:
                    idx = min(len(route)-1, max(0, int(frac * (len(route)-1))))
                    item.update({'current_lat': route[idx][0], 'current_lng': route[idx][1]})
                else:
                    item.update({'current_lat': pickup_lat + (hub_lat - pickup_lat) * frac, 'current_lng': pickup_lng + (hub_lng - pickup_lng) * frac})
        out.append(item)
    return jsonify({'orders': out})

@app.route('/update_status/<int:order_id>', methods=['POST'])
def update_status(order_id):
    db = get_db()
    db.execute('UPDATE orders SET status=? WHERE id=?', ('Delivered', order_id))
    db.commit()
    return jsonify({'message':'updated'})

@app.route('/delete_order/<int:order_id>', methods=['POST'])
def delete_order(order_id):
    db = get_db()
    db.execute('DELETE FROM orders WHERE id=?', (order_id,))
    db.commit()
    return jsonify({'message':'deleted'})

# ---------------- CSV Export / Import (Admin) ----------------
@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username','')
        password = request.form.get('password','')
        if username == ADMIN_USER and password == ADMIN_PASS:
            session['logged_in'] = True
            session.permanent = True
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid credentials')
            return render_template_string(ADMIN_LOGIN_HTML)
    return render_template_string(ADMIN_LOGIN_HTML)

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/admin')
@require_admin
def admin_dashboard():
    return render_template_string(ADMIN_DASH_HTML)

@app.route('/export_csv')
@require_admin
def export_csv():
    db = get_db()
    cur = db.execute('SELECT * FROM orders')
    rows = cur.fetchall()
    # prepare CSV in-memory
    si = io.StringIO()
    cw = csv.writer(si)
    header = ['id','package_name','pickup_lat','pickup_lng','hub_lat','hub_lng','distance_m','speed_mps','created_at','start_time','duration_s','status']
    cw.writerow(header)
    for r in rows:
        cw.writerow([r['id'], r['package_name'], r['pickup_lat'], r['pickup_lng'], r['hub_lat'], r['hub_lng'], r['distance_m'], r['speed_mps'], r['created_at'], r['start_time'], r['duration_s'], r['status']])
    mem = io.BytesIO()
    mem.write(si.getvalue().encode('utf-8'))
    mem.seek(0)
    si.close()
    return send_file(mem, as_attachment=True, download_name='orders_export.csv', mimetype='text/csv')

@app.route('/import_csv', methods=['POST'])
@require_admin
def import_csv():
    if 'file' not in request.files:
        flash('No file provided')
        return redirect(url_for('admin_dashboard'))
    f = request.files['file']
    if f.filename == '':
        flash('No selected file')
        return redirect(url_for('admin_dashboard'))
    if not allowed_file(f.filename):
        flash('Only CSV files allowed')
        return redirect(url_for('admin_dashboard'))

    filename = secure_filename(f.filename)
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    f.save(path)
    # parse CSV
    imported = 0
    with open(path, newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        # expected columns: package_name,pickup_lat,pickup_lng,hub_lat,hub_lng,speed_mps
        for row in reader:
            try:
                pkg = row.get('package_name', 'Package')
                pl = float(row['pickup_lat']); plng = float(row['pickup_lng'])
                hl = float(row['hub_lat']); hlng = float(row['hub_lng'])
                speed = float(row.get('speed_mps', row.get('speed', 8.0)))
            except Exception:
                continue
            dist = haversine_m(pl,plng,hl,hlng)
            duration = dist / speed if speed>0 else None
            route_pts = generate_route_with_turns(pl,plng,hl,hlng, points=18)
            db = get_db()
            db.execute('INSERT INTO orders (package_name,pickup_lat,pickup_lng,hub_lat,hub_lng,distance_m,speed_mps,created_at,start_time,duration_s,status,route_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (pkg,pl,plng,hl,hlng,dist,speed,time.time(),None,duration,'Pending', json.dumps(route_pts)))
            db.commit()
            imported += 1
    flash(f'Imported {imported} rows.')
    return redirect(url_for('admin_dashboard'))

# ---------------- Run ----------------
if __name__ == '__main__':
    print(f"Starting Smart Drone Delivery app — open http://127.0.0.1:{APP_PORT}")
    app.run(debug=True, host='0.0.0.0', port=APP_PORT)
