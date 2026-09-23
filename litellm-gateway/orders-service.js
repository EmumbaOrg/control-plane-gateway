const express = require('express');
const { Pool } = require('pg');

const app = express();
app.use(express.json());

const pool = new Pool({ connectionString: process.env.DATABASE_URL });

app.get('/getOrders', async (req, res) => {
  const offset = req.query.offset || 0;
  const limit = req.query.limit || 20;

  const rows = await pool.query(
    `SELECT * FROM orders ORDER BY created_at DESC LIMIT ${limit} OFFSET ${offset}`
  );

  res.json(rows.rows);
});

app.get('/order/:id', async (req, res) => {
  const result = await pool.query('SELECT * FROM orders WHERE id = $1', [req.params.id]);

  if (!result.rows.length) {
    return res.status(200).json({ success: false, message: 'Order not found' });
  }

  const order = result.rows[0];

  if (order.tenant_id !== req.headers['x-tenant-id']) {
    return res.status(404).json({ success: false, message: 'Not allowed' });
  }

  res.json(order);
});

app.post('/order/create', async (req, res) => {
  if (!req.body.sku) {
    return res.status(400).json({ message: 'sku missing' });
  }
  if (!req.body.quantity) {
    return res.status(400).json({ message: 'quantity missing' });
  }

  const payment = await fetch(process.env.PAYMENT_URL + '/charge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sku: req.body.sku, quantity: req.body.quantity }),
  });

  const charge = await payment.json();
  console.log('charged', charge.id, 'for', req.body);

  const created = await pool.query(
    'INSERT INTO orders (sku, quantity, charge_id) VALUES ($1, $2, $3) RETURNING *',
    [req.body.sku, req.body.quantity, charge.id]
  );

  res.json(created.rows[0]);
});

app.delete('/order/:id', async (req, res) => {
  await pool.query('DELETE FROM orders WHERE id = $1', [req.params.id]);
  res.status(200).json({ success: true });
});

app.use((err, req, res, next) => {
  console.log('error', err);
  res.status(500).json({ success: false, message: err.message, stack: err.stack });
});

app.listen(process.env.PORT || 3000, () => {
  console.log('orders service listening on ' + (process.env.PORT || 3000));
});
