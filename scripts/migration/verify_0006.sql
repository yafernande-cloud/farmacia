-- Verificaciones POSTERIORES a 0006. El bloque DO lanza una excepción si algo no se cumple.
DO $$
DECLARE legacy uuid; sim uuid; real uuid; n int;
BEGIN
  SELECT id INTO legacy FROM organization WHERE kind='legacy_mixed';
  SELECT id INTO sim    FROM organization WHERE kind='simulation';
  SELECT id INTO real   FROM organization WHERE kind='real_test';
  IF legacy IS NULL OR sim IS NULL OR real IS NULL THEN RAISE EXCEPTION 'faltan organizaciones'; END IF;

  -- nada quedó sin clasificar
  SELECT count(*) INTO n FROM (
    SELECT data_origin FROM demand_observation UNION ALL SELECT data_origin FROM demand_item UNION ALL
    SELECT data_origin FROM channel_message UNION ALL SELECT data_origin FROM attachment UNION ALL
    SELECT data_origin FROM pharma_identity) t WHERE data_origin='unclassified';
  IF n <> 0 THEN RAISE EXCEPTION '% filas siguen unclassified', n; END IF;

  -- procedencia coherente con el chat de origen
  IF EXISTS (SELECT 1 FROM demand_observation o JOIN channel_message m ON m.id=o.channel_message_id JOIN channel_binding b ON b.id=m.channel_binding_id
             WHERE (b.external_chat_id ~ '^555000' AND o.data_origin <> 'simulated') OR (b.external_chat_id !~ '^555000' AND o.data_origin <> 'captured'))
  THEN RAISE EXCEPTION 'procedencia de observaciones incoherente con su chat'; END IF;

  -- ninguna necesidad mezcla procedencias
  IF EXISTS (SELECT 1 FROM demand_item i JOIN demand_observation o ON o.demand_item_id=i.id
             GROUP BY i.id HAVING count(DISTINCT o.data_origin) > 1 OR min(o.data_origin) <> max(i.data_origin))
  THEN RAISE EXCEPTION 'una necesidad sigue mezclando procedencias'; END IF;

  -- la necesidad separada: las 2 observaciones reales (Ana, Luis) cuelgan de UNA necesidad propia, sin cantidad
  SELECT count(*) INTO n FROM demand_observation o JOIN app_user u ON u.id=o.author_id
    WHERE o.data_origin='captured' AND u.display_name IN ('Ana','Luis') AND o.demand_item_id IS NOT NULL;
  IF n <> 2 THEN RAISE EXCEPTION 'se esperaban 2 observaciones reales de Ana y Luis, hay %', n; END IF;
  IF EXISTS (SELECT 1 FROM demand_item WHERE target_quantity IS NOT NULL) THEN RAISE EXCEPTION 'apareció una cantidad'; END IF;
  IF (SELECT count(DISTINCT demand_item_id) FROM demand_observation WHERE data_origin='captured') <> 1 THEN RAISE EXCEPTION 'las observaciones reales no comparten una necesidad'; END IF;

  -- vínculos de chat: antiguos archivados, nuevos activos en su organización
  IF (SELECT count(*) FROM channel_binding WHERE status='active') <> 2 THEN RAISE EXCEPTION 'debe haber exactamente 2 vínculos activos'; END IF;
  IF NOT EXISTS (SELECT 1 FROM channel_binding WHERE external_chat_id='5550001' AND status='active' AND organization_id=sim) THEN RAISE EXCEPTION '5550001 no está en simulación'; END IF;
  IF NOT EXISTS (SELECT 1 FROM channel_binding WHERE external_chat_id='6358481229' AND status='active' AND organization_id=real) THEN RAISE EXCEPTION '6358481229 no está en prueba real'; END IF;
  IF (SELECT count(*) FROM channel_binding WHERE status='inactive' AND organization_id=legacy) <> 2 THEN RAISE EXCEPTION 'los vínculos antiguos no quedaron archivados'; END IF;

  -- las organizaciones nuevas no heredan datos de negocio
  SELECT count(*) INTO n FROM (SELECT organization_id FROM demand_item UNION ALL SELECT organization_id FROM demand_observation
                               UNION ALL SELECT organization_id FROM pharma_identity) t WHERE organization_id IN (sim, real);
  IF n <> 0 THEN RAISE EXCEPTION 'las organizaciones nuevas no están vacías (%)', n; END IF;

  -- el personal nuevo existe en ambas y el archivo conserva a Ana y Luis
  IF (SELECT count(*) FROM app_user WHERE organization_id IN (sim, real)) <> 10 THEN RAISE EXCEPTION 'faltan personas en las organizaciones nuevas'; END IF;
  IF (SELECT count(*) FROM app_user WHERE organization_id = legacy AND display_name IN ('Ana','Luis','Pao')) <> 3 THEN RAISE EXCEPTION 'el archivo perdió personal histórico'; END IF;

  -- auditoría de la migración y sin sesiones abiertas
  IF (SELECT count(*) FROM audit_event WHERE correlation_id='migration-0006') <> 2 THEN RAISE EXCEPTION 'faltan filas de auditoría de la migración'; END IF;
  IF EXISTS (SELECT 1 FROM actor_session WHERE ended_at IS NULL) THEN RAISE EXCEPTION 'quedó una sesión abierta'; END IF;
END $$;

SELECT 'OK: verificaciones posteriores superadas' AS resultado;
SELECT o.name organizacion, o.kind, (SELECT count(*) FROM app_user u WHERE u.organization_id=o.id) personas,
       (SELECT count(*) FROM demand_observation x WHERE x.organization_id=o.id) observaciones,
       (SELECT count(*) FROM demand_item x WHERE x.organization_id=o.id) necesidades,
       (SELECT string_agg(external_chat_id||'('||status||')', ',') FROM channel_binding b WHERE b.organization_id=o.id) chats
FROM organization o ORDER BY o.kind;
SELECT i.display_name necesidad, i.data_origin, i.target_quantity cantidad, i.created_at,
       string_agg(u.display_name||' '||to_char(o.created_at,'HH24:MI:SS')||' '||o.data_origin, ' · ' ORDER BY o.created_at) observaciones
FROM demand_item i JOIN demand_observation o ON o.demand_item_id=i.id JOIN app_user u ON u.id=o.author_id GROUP BY i.id ORDER BY i.created_at;
