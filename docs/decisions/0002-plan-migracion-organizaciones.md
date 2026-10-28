# Plan de migración: separar SIMULACIÓN / PRUEBA REAL / ARCHIVO

**Estado: PLAN — pendiente de autorización. La base `farmacia` NO se ha modificado** (sigue en la revisión
Alembic `0003`). Todo lo descrito aquí se ensayó sobre una **copia** (`farmacia_rehearsal`, restaurada de un respaldo).

## 1. Qué hay hoy (lectura de solo consulta, 2026-10-28)

Una sola organización («Farmacia Parque Lantaño (demo)») con 3 usuarios compartidos (Ana, Luis, Pao), 1 ciclo abierto
compartido y 2 chats: `5550001` (mi demo con Telegram falso) y `6358481229` (su prueba real).

| Tabla | Demo (5550001) | Prueba real (6358481229) |
|---|---|---|
| inbound_event | 13 | 8 |
| channel_message | 7 | 3 |
| actor_session | 2 | 2 |
| outbound_message | 18 | 10 |
| attachment | 3 (2 con lectura simulada) | 1 (Migranol, `pending`) |
| demand_observation | 5 | 2 (Ana 18:31:51, Luis 18:32:57) |
| audit_event / outbox_event | 20 / 20 | 6 / 6 |
| demand_item | 3 (Vitamina D, Migranol, Vitamina D3) | **0 propias**: las 2 observaciones reales cuelgan de la necesidad de la demo |
| pharma_identity | 1 (creada por la demo) | — |

Contaminaciones confirmadas: (a) las 2 observaciones reales se asociaron a la necesidad de la demo (4 avisos en total);
(b) el catálogo «verificado» de la demo aparece como candidato en ambos avisos reales; (c) usuarios y ciclo compartidos.

## 2. Por qué no se puede «mover filas» sin más

Todo vínculo es una clave foránea **compuesta** `(id, organization_id)` y no es diferible; los usuarios (Ana, Luis, Pao) son
UNA fila con UN id que no puede pertenecer a dos organizaciones; `audit_event`/`quantity_event` son de solo agregar y
`demand_observation.author_id` es inmutable por trigger. Mover filas entre organizaciones obligaría a **desactivar triggers**
(sobre historia inmutable) y a **cambiar autores** de observaciones.

## 3. Opciones evaluadas

| | A · ARCHIVO (recomendada) | B · Mover lo real a la org. real | C · Mover la demo a la org. de simulación |
|---|---|---|---|
| Filas que se mueven de organización | **0** | todas las reales + usuarios Ana/Luis/Pao | todas las de la demo |
| Triggers a desactivar | **ninguno** | sí (autoría, solo-agregar) | sí |
| Autorías que cambian | **ninguna** | las de la demo (usuarios clonados) | las reales (usuarios clonados) |
| Ids originales | **todos intactos** | cambian los de usuarios en la demo | cambian los de usuarios reales |
| Reversible | **sí (restaurar respaldo; o deshacer a mano)** | difícil | difícil |
| La org. real arranca con el historial | no (queda en el archivo) | sí | sí |

**Recomendación: A.** La organización actual queda como **ARCHIVO** (`legacy_mixed`) con todo su historial; las dos
organizaciones nuevas (simulación y prueba real) nacen vacías, con el personal real y sus chats. Se paga con una sola cosa:
la historia de la prueba del 28-oct (Ana, Luis, Migranol) se consulta en el archivo, no en la organización real nueva.
Si prefieres que esa historia viva en la organización real, B es posible pero exige tocar triggers e ids: lo desaconsejo.

## 4. Qué haría exactamente (opción A) — `scripts/migration/0006_separar_organizaciones.sql`

Una transacción, con guardas que **abortan sin cambiar nada** si el estado no es el esperado (y se niega a correr dos veces).

1. Renombrar la organización actual a «…ARCHIVO 2026-10-28 (demo + prueba real)» (sigue `legacy_mixed`).
2. Crear las organizaciones «SIMULACIÓN» (`simulation`) y «Prueba real» (`real_test`), cada una con su ciclo.
3. Clasificar `data_origin` de lo existente (solo se escribe esa columna): chat `555000x` → `simulated`; otro → `captured`.
   `pharma_identity` (1 fila, creada 18:06:47 antes del chat real) → `simulated`.
4. **Separar la necesidad «Vitamina D»**: crear una necesidad nueva (`captured`, fecha = la de la primera observación real,
   sin cantidad) y apuntar a ella las 2 observaciones reales. Solo cambia `demand_observation.demand_item_id`.
   La necesidad original queda con las 2 observaciones de la demo (`simulated`).
5. Archivar los vínculos antiguos (`status='inactive'`) y crear los nuevos: `5550001`→simulación, `6358481229`→prueba real.
6. Cerrar sesiones aún abiertas del archivo; escribir 2 filas de auditoría (`migration.item_split`, `migration.org_separation`).

Después: `python -m app.cli seed-demo` y `seed-real-test` cargan Pao, Danny, Julio, Iván y Pato (desde `database/seed/staff.json`) en cada
organización nueva. Ana y Luis permanecen únicamente en el archivo.

### Filas afectadas
| Acción | Filas |
|---|---|
| UPDATE `data_origin` | observaciones 7, necesidades 3, mensajes 10, fotos 4, catálogo 1 |
| UPDATE `demand_observation.demand_item_id` | **2** (las reales) |
| UPDATE `channel_binding.status` | 2 |
| UPDATE `organization.name` | 1 |
| UPDATE `actor_session` (cierre) | las que estén abiertas (0–2) |
| INSERT | 2 organizaciones, 2 ciclos, 1 necesidad, 2 vínculos, 2 auditorías, 10 personas |
| DELETE / cancelaciones / cambios de autor, fecha, texto o cantidad | **0** |

## 5. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Un proceso escribe durante la migración | **Detener `telegram-poll` antes** (Ctrl+C) y no reiniciarlo hasta terminar. La transacción además es atómica |
| El poller reiniciado con el código nuevo sobre el esquema viejo falla | Aplicar esquema y datos **antes** de reiniciar el poller |
| Pérdida de datos | Respaldo `pg_dump -Fc` (hecho: `.backups/farmacia_pre_separacion_20261028_160900.dump`, SHA-256 `c492a635…594d656`) + ensayo en copia |
| Los eventos del outbox/auditoría siguen mencionando la necesidad original | Es historia: no se reescribe. La auditoría `migration.item_split` documenta el vínculo |
| Una foto real (Migranol) ya guardada | Queda en `.storage/` con su referencia; sigue ligada a su mensaje en el archivo. Para procesarla con visión real habrá que reenviarla a la organización nueva |
| Quien usaba la cabecera `X-Demo-User-Id` con ids de Ana/Luis | Esos ids siguen válidos pero solo en el archivo; la org. real tiene ids nuevos |
| Aplicar los datos sin el esquema 0004 | El script aborta (las columnas no existen) |

## 6. Respaldo, ejecución y verificaciones

```bash
# 0) detener el poller (Ctrl+C en su terminal)
# 1) respaldo (ya existe; repetir justo antes si pasa tiempo)
docker exec database-postgres-1 pg_dump -U farmacia -Fc farmacia > .backups/farmacia_pre_separacion_$(date +%Y%m%d_%H%M%S).dump
# 2) ensayo (opcional, repetible): scripts/migration/rehearse.sh .backups/<dump>
# 3) esquema (aditivo): cd backend && .venv/bin/alembic upgrade head          # 0003 -> 0005
# 4) huella antes:  psql … -f scripts/migration/fingerprint.sql > /tmp/fp_before.txt
# 5) datos:         psql … -v ON_ERROR_STOP=1 -f scripts/migration/0006_separar_organizaciones.sql
# 6) personal:      .venv/bin/python -m app.cli seed-demo && .venv/bin/python -m app.cli seed-real-test
# 7) huella después y comparación; verify_0006.sql
```
Verificaciones (ya pasan en el ensayo): huella md5 idéntica de observaciones, mensajes, fotos, eventos, sesiones, usuarios del archivo,
auditoría previa y outbox (autoría, fechas, textos, cantidades, referencias); ninguna fila `unclassified`; ninguna necesidad mezcla
procedencias; las 2 observaciones de Ana y Luis comparten una necesidad propia y sin cantidad; exactamente 2 vínculos activos
(5550001→simulación, 6358481229→real); organizaciones nuevas sin datos heredados; 5 personas en cada una; el archivo conserva Ana, Luis y Pao.
Comprobado en la copia: el chat real resuelve a «Prueba real» con los 5 botones desde PostgreSQL; el chat ficticio no puede vincularse a una
organización real ni el tipo de una organización cambiarse.

**Reversión:** restaurar el respaldo (`pg_restore -c`), o `alembic downgrade 0003` si no se aplicó 0006 (0006 no tiene downgrade: se revierte con el respaldo).

## 7. Pendiente de tu autorización
1. ¿Opción A (recomendada)? 2. ¿Apruebas aplicar `0004` y `0005` (esquema aditivo) y `0006` (datos) en `farmacia`?
3. Confirmar que detendrás `telegram-poll` durante la migración.
