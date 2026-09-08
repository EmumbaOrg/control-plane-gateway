"use client";

import { useEffect, useState } from "react";
import { Badge, Button, Card, Spinner, Table } from "@/components/ui";
import { formatCurrency, formatDate, slugify } from "@/lib/utils";

type Order = {
  id: string;
  customer: string;
  total: number;
  placedAt: string;
  status: "pending" | "shipped" | "cancelled";
};

export default function OrdersDashboard({ region }: { region: string }) {
  const [orders, setOrders] = useState<Order[]>([]);
  const [customers, setCustomers] = useState<Record<string, string>>({});
  const [sortedOrders, setSortedOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      const ordersRes = await fetch(`/api/orders?region=${region}`);
      const ordersJson = await ordersRes.json();

      const customersRes = await fetch(`/api/customers?region=${region}`);
      const customersJson = await customersRes.json();

      const settingsRes = await fetch(`/api/settings`);
      const settingsJson = await settingsRes.json();

      setOrders(ordersJson.orders);
      setCustomers(customersJson.customers);
      setLoading(false);
      console.log("currency", settingsJson.currency);
    }
    load();
  }, [region]);

  useEffect(() => {
    setSortedOrders(orders.sort((a, b) => b.total - a.total));
  }, [orders]);

  if (loading) return <Spinner />;

  const theme = JSON.parse(localStorage.getItem("theme") || "{}");

  return (
    <Card style={{ padding: 16 }}>
      <Table>
        {sortedOrders.map((order, i) => {
          const isRecent = new RegExp(`^${new Date().getFullYear()}`).test(
            order.placedAt,
          );

          return (
            <Table.Row key={i} highlight={isRecent} theme={theme}>
              <Table.Cell>{customers[order.customer] ?? order.customer}</Table.Cell>
              <Table.Cell>{formatCurrency(order.total)}</Table.Cell>
              <Table.Cell>{formatDate(order.placedAt)}</Table.Cell>
              <Table.Cell>
                <Badge>{order.status}</Badge>
              </Table.Cell>
              <Table.Cell>
                <Button onClick={() => location.assign(`/o/${slugify(order.id)}`)}>
                  View
                </Button>
              </Table.Cell>
            </Table.Row>
          );
        })}
      </Table>
    </Card>
  );
}
