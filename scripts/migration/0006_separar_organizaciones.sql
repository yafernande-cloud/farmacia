-- ============================================================================================
-- 0006 · Separación de datos: SIMULACIÓN / PRUEBA REAL / ARCHIVO  (PLAN "ARCHIVO", sin mover filas)
-- ============================================================================================
-- Requiere el esquema 0005 aplicado. UNA transacción: o se aplica todo o nada.
-- NO borra, NO cancela y NO reasigna autorías. Qué hace:
--   1. La organización actual queda como ARCHIVO (kind=legacy_mixed): conserva TODO su historial, sus
--      usuarios (Ana, Luis, Pao) y sus ids. Solo cambia su nombre.
--   2. Crea la organización de SIMULACIÓN y la de PRUEBA REAL (con su ciclo abierto cada una).
--   3. Clasifica la procedencia de las filas existentes (solo se escribe la columna data_origin).
--   4. Separa la necesidad «Vitamina D» mezclada: las observaciones REALES (Ana, Luis) pasan a una necesidad
--      nueva; las de la demo quedan en la original. Solo cambia demand_observation.demand_item_id (ni
--      fechas, ni autores, ni textos, ni cantidades).
--   5. Archiva los vínculos de chat antiguos (status='inactive') y crea los nuevos: 5550001 → simulación,
--      6358481229 → prueba real.
--   6. Deja una fila de auditoría que documenta la migración.
-- El personal (Pao, Danny, Julio, Iván, Pato) se carga DESPUÉS con la CLI desde database/seed/staff.json.
-- Ningún trigger se desactiva y ninguna restricción se relaja.
-- ============================================================================================
BEGIN;

-- 0) Guardas: el script solo corre sobre el estado esperado (si algo difiere, aborta sin cambiar nada)
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM organization;
  IF n <> 1 THEN RAISE EXCEPTION 'se esperaba 1 organización, hay %', n; END IF;
  IF (SELECT kind FROM organization) <> 'legacy_mixed' THEN RAISE EXCEPTION 'la organización actual no es legacy_mixed'; END IF;
  SELECT count(*) INTO n FROM channel_binding WHERE external_chat_id IN ('5550001','6358481229') AND status='active';
  IF n <> 2 THEN RAISE EXCEPTION 'se esperaban los 2 chats activos (5550001, 6358481229), hay %', n; END IF;
  IF EXISTS (SELECT 1 FROM organization WHERE kind IN ('simulation','real_test')) THEN RAISE EXCEPTION 'ya existe una organización simulation/real_test'; END IF;
  IF (SELECT count(*) FROM demand_observation WHERE data_origin <> 'unclassified') > 0 THEN RAISE EXCEPTION 'el esquema ya fue clasificado'; END IF;
END $$;

-- 1) La organización actual pasa a ser el ARCHIVO
UPDATE organization SET name = 'Farmacia Parque Lantaño — ARCHIVO 2026-10-28 (demo + prueba real)' WHERE kind = 'legacy_mixed';

-- 2) Organizaciones nuevas, con su ciclo
INSERT INTO organization (name, kind) VALUES
  ('Farmacia Parque Lantaño — SIMULACIÓN', 'simulation'),
  ('Farmacia Parque Lantaño — Prueba real', 'real_test');
INSERT INTO purchase_cycle (organization_id, name)
  SELECT id, CASE kind WHEN 'simulation' THEN 'Ciclo de simulación' ELSE 'Ciclo de prueba real' END
  FROM organization WHERE kind IN ('simulation','real_test');

-- 3) Procedencia de lo existente. Regla: el chat de origen. 555000x = simulado; cualquier otro = capturado.
UPDATE channel_message m SET data_origin = CASE WHEN b.external_chat_id ~ '^555000[0-9]+$' THEN 'simulated' ELSE 'captured' END
  FROM channel_binding b WHERE b.id = m.channel_binding_id;
UPDATE attachment a SET data_origin = m.data_origin FROM channel_message m WHERE m.id = a.channel_message_id;
UPDATE demand_observation o SET data_origin = m.data_origin FROM channel_message m WHERE m.id = o.channel_message_id;
-- necesidades: la procedencia de la observación que las creó
UPDATE demand_item i SET data_origin = o.data_origin FROM demand_observation o
  WHERE o.demand_item_id = i.id AND o.match_status = 'created_item';
-- catálogo verificado: lo creado antes de que existiera el chat real es de la demo (simulado)
UPDATE pharma_identity p SET data_origin = CASE WHEN p.created_at < (SELECT min(created_at) FROM channel_binding WHERE external_chat_id !~ '^555000[0-9]+$')
                                               THEN 'simulated' ELSE 'captured' END;
UPDATE commercial_reference c SET data_origin = 'simulated';
UPDATE product_alias a SET data_origin = 'simulated';

-- 4) Separar necesidades mezcladas (con observaciones simuladas y reales)
CREATE TEMP TABLE _split (old_item uuid, new_item uuid, first_real timestamptz) ON COMMIT DROP;
INSERT INTO _split
  SELECT i.id, gen_random_uuid(), min(o.created_at) FILTER (WHERE o.data_origin = 'captured')
  FROM demand_item i JOIN demand_observation o ON o.demand_item_id = i.id
  GROUP BY i.id HAVING count(DISTINCT o.data_origin) > 1;
INSERT INTO demand_item (id, organization_id, cycle_id, status, display_name, ingredient, attributes, resolved_identity_key,
                         pharma_identity_key, target_quantity, quantity_unit, version, created_at, updated_at, data_origin)
  SELECT s.new_item, i.organization_id, i.cycle_id, i.status, i.display_name, i.ingredient, i.attributes, NULL, NULL,
         i.target_quantity, i.quantity_unit, 1, s.first_real, s.first_real, 'captured'
  FROM _split s JOIN demand_item i ON i.id = s.old_item;
UPDATE demand_observation o SET demand_item_id = s.new_item FROM _split s
  WHERE o.demand_item_id = s.old_item AND o.data_origin = 'captured';
UPDATE demand_item i SET data_origin = 'simulated' FROM _split s WHERE i.id = s.old_item;  -- la original queda con lo simulado
INSERT INTO audit_event (organization_id, actor_id, action, entity_type, entity_id, before, after, correlation_id)
  SELECT i.organization_id, NULL, 'migration.item_split', 'demand_item', s.new_item,
         jsonb_build_object('original_item', s.old_item),
         jsonb_build_object('moved_observations', (SELECT jsonb_agg(o.id ORDER BY o.created_at) FROM demand_observation o WHERE o.demand_item_id = s.new_item)),
         'migration-0006'
  FROM _split s JOIN demand_item i ON i.id = s.new_item;

-- 5) Vínculos de chat: se archivan los antiguos y se crean los nuevos (el índice único es solo entre activos)
UPDATE channel_binding SET status = 'inactive' WHERE external_chat_id IN ('5550001','6358481229');
INSERT INTO channel_binding (organization_id, external_chat_id)
  SELECT id, CASE kind WHEN 'simulation' THEN '5550001' ELSE '6358481229' END FROM organization WHERE kind IN ('simulation','real_test');

-- 6) Sesiones que sigan abiertas en el archivo: se cierran (el chat ya no pertenece a esa organización)
UPDATE actor_session SET ended_at = now(), end_reason = 'manual' WHERE ended_at IS NULL;

INSERT INTO audit_event (organization_id, actor_id, action, entity_type, entity_id, after, correlation_id)
  SELECT id, NULL, 'migration.org_separation', 'organization', id,
         jsonb_build_object('plan', 'archivo', 'script', '0006_separar_organizaciones.sql'), 'migration-0006'
  FROM organization WHERE kind = 'legacy_mixed';
COMMIT;
