-- 0009 — Seed configurable material categories (editable by IT Admin later).

insert into material_categories (name, kind, sort_order) values
  ('General Material',        'material', 10),
  ('Switchgear',             'material', 20),
  ('Generator & Equipment',  'material', 30),
  ('Lighting',               'material', 40),
  ('Low Voltage',            'material', 50),
  ('Saw-cut Markup',         'markup',   60),
  ('Trench Markup',          'markup',   70)
on conflict do nothing;
