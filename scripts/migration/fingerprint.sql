-- Huella de lo que NO debe cambiar (autoría, fechas, textos, cantidades, referencias). Se compara antes/después.
SELECT 'observaciones' t, count(*) n, md5(string_agg(concat_ws('|', id, organization_id, author_id, created_at, raw_text, parsed::text, declared_quantity, declared_unit, match_status, idempotency_key, channel_message_id, attachment_id, actor_session_id), ';' ORDER BY id)) h FROM demand_observation
UNION ALL SELECT 'channel_message', count(*), md5(string_agg(concat_ws('|', id, author_id, text, kind, attribution_status, session_id, sent_at, received_at), ';' ORDER BY id)) FROM channel_message
UNION ALL SELECT 'attachment', count(*), md5(string_agg(concat_ws('|', id, author_id, content_sha256, storage_key, status, caption, extraction::text), ';' ORDER BY id)) FROM attachment
UNION ALL SELECT 'inbound_event', count(*), md5(string_agg(concat_ws('|', id, external_event_id, payload::text, status, received_at), ';' ORDER BY id)) FROM inbound_event
UNION ALL SELECT 'outbound_message', count(*), md5(string_agg(concat_ws('|', id, payload::text, status, provider_message_id), ';' ORDER BY id)) FROM outbound_message
UNION ALL SELECT 'actor_session(sin cierre)', count(*), md5(string_agg(concat_ws('|', id, auxiliary_id, started_at, last_activity_at, expires_at), ';' ORDER BY id)) FROM actor_session
UNION ALL SELECT 'app_user', count(*), md5(string_agg(concat_ws('|', id, organization_id, display_name, roles::text, active), ';' ORDER BY id)) FROM app_user WHERE organization_id IN (SELECT id FROM organization WHERE kind = 'legacy_mixed')
UNION ALL SELECT 'audit_event(previos)', count(*), md5(string_agg(concat_ws('|', id, actor_id, action, entity_id, created_at), ';' ORDER BY id)) FROM audit_event WHERE correlation_id NOT LIKE 'migration-%'
UNION ALL SELECT 'outbox_event', count(*), md5(string_agg(concat_ws('|', seq, event_id, type, payload::text, occurred_at), ';' ORDER BY seq)) FROM outbox_event
UNION ALL SELECT 'quantity_event', count(*), '' FROM quantity_event
UNION ALL SELECT 'cantidades en necesidades', count(*) FILTER (WHERE target_quantity IS NOT NULL), '' FROM demand_item;
