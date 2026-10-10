PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    phone_number TEXT,
    is_demo INTEGER NOT NULL DEFAULT 1 CHECK (is_demo IN (0, 1))
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    user_phonenumber TEXT,
    order_status TEXT NOT NULL CHECK (order_status IN ('pending_payment','paid','completed','cancelled','unknown')),
    shipping_status TEXT NOT NULL CHECK (shipping_status IN ('unshipped','partially_shipped','shipped','unknown')),
    amount_fen INTEGER CHECK (amount_fen IS NULL OR amount_fen >= 0),
    created_at TEXT,
    paid_at TEXT,
    shipped_at TEXT,
    source TEXT NOT NULL DEFAULT 'demo',
    legacy_tracking_number TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(user_phonenumber);
CREATE TABLE IF NOT EXISTS order_items (
    item_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    sku_id TEXT,
    item_title TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0)
);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    tracking_number TEXT,
    carrier_name TEXT,
    logistics_status TEXT NOT NULL CHECK (logistics_status IN ('no_records','in_transit','delivering','delivered','exception','unknown')),
    latest_status_desc TEXT,
    estimated_delivery_at TEXT,
    eta_source TEXT,
    data_source TEXT NOT NULL DEFAULT 'demo',
    updated_at TEXT,
    carrier_code TEXT NOT NULL DEFAULT 'mock_express'
);
CREATE INDEX IF NOT EXISTS idx_shipments_order ON shipments(order_id);
CREATE INDEX IF NOT EXISTS idx_shipments_tracking ON shipments(tracking_number);
CREATE TABLE IF NOT EXISTS tracking_events (
    event_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    status_time TEXT NOT NULL,
    status_desc TEXT NOT NULL,
    location TEXT
);
CREATE TABLE IF NOT EXISTS legacy_order_import (
    import_id TEXT PRIMARY KEY,
    raw_record_json TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    imported_at TEXT NOT NULL,
    needs_review INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS human_requests (
    request_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    order_id TEXT REFERENCES orders(order_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','accepted','failed','not_connected')),
    external_ticket_id TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    transfer_state TEXT NOT NULL DEFAULT 'not_connected',
    queue_position INTEGER,
    agent_name TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS demo_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    params_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_request_conversations (
    request_id TEXT PRIMARY KEY REFERENCES human_requests(request_id),
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
);
CREATE TABLE IF NOT EXISTS courier_api_calls (
    request_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    user_id TEXT NOT NULL REFERENCES users(user_id),
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    endpoint TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    result_status TEXT NOT NULL,
    http_status INTEGER,
    elapsed_ms INTEGER NOT NULL,
    called_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_courier_calls_user ON courier_api_calls(user_id,called_at);
CREATE TABLE IF NOT EXISTS human_api_calls (
    call_id TEXT PRIMARY KEY,
    human_request_id TEXT NOT NULL REFERENCES human_requests(request_id),
    user_id TEXT NOT NULL REFERENCES users(user_id),
    endpoint TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    result_status TEXT NOT NULL,
    http_status INTEGER,
    elapsed_ms INTEGER NOT NULL,
    called_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_human_calls_user ON human_api_calls(user_id,called_at);
