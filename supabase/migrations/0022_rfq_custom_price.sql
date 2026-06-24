-- 0022 — Per-RFQ custom price (receive-quotes step).
--
-- The PE can price a category with a custom number instead of any vendor
-- quote. Mutually exclusive with an explicitly selected quote: setting a
-- custom price clears is_selected on the RFQ's quotes, and selecting a quote
-- clears the custom price. Pricing precedence: custom > selected > lowest.

alter table rfqs
  add column custom_amount  numeric(14,2),
  add column custom_set_by  uuid references profiles(id) on delete set null,
  add column custom_set_at  timestamptz;
