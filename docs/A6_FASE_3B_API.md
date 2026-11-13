# A6 Fase 3B — API REST de observaciones comerciales e interpretación

La API guarda e interpreta las observaciones que se ingresan desde «Enseñar a la IA» en Lovable.
La suite habitual usa un proveedor simulado.
Complementa el contrato `docs/contracts/cotizador-v0.2.md` §8.3 (espacio
`/api/v1/commercial-observations`, D0/rev10-A6) y no crea un namespace nuevo.

## 1. Rutas

| Método | Ruta | Para qué |
| --- | --- | --- |
| `POST` | `/api/v1/commercial-observations` | Guardar el texto libre de una observación del artículo (O1/O2). |
| `POST` | `/api/v1/commercial-observations/{observation_id}/interpretations` | Interpretar con el servicio interno de Fase 3A, o devolver el intento ya procesado. |
| `GET` | `/api/v1/commercial-observations/{observation_id}` | Detalle: observación, todos sus intentos y sus preferencias. |
| `GET` | `/api/v1/commercial-observations?trade_item_id=…` | Historial paginado del artículo exacto. |
| `GET` | `/api/v1/articles/{article_id}/preferences` | Preferencias vigentes, retiros e historial del artículo. |

No existen rutas `/articles/{id}/observations`: se reutiliza el namespace E13 ya definido en el
contrato. La observación y el artículo siempre se buscan con la organización del `Actor`.

## 2. Ejemplos

### 2.1 Guardar

```http
POST /api/v1/commercial-observations
Authorization: <JWT de Supabase o cabeceras X-Demo-* en AUTH_MODE=demo_header>
Idempotency-Key: 8fd1c2a4-observacion-0001   # opcional
Content-Type: application/json

{ "text": "Prefiero Laboratorio Chile aunque cueste un poco más", "trade_item_id": "6d1f…" }
```

`201 Created`:

```json
{
  "observation_id": "0b7e…",
  "text": "Prefiero Laboratorio Chile aunque cueste un poco más",
  "trade_item_id": "6d1f…",
  "supplier_id": null,
  "offer_snapshot_id": null,
  "quote_id": null,
  "quote_line_id": null,
  "author_id": "83c0…",
  "created_at": "2026-11-03T18:04:21Z",
  "context": { "trade_item": { "id": "6d1f…", "display_name": "Losartán 50 mg…", "brand": null, "laboratory": null } },
  "data_origin": "captured",
  "interpretation_status": "sin_interpretacion",
  "interpretation": null,
  "interpretations": []
}
```

Solo se aceptan `text`, `trade_item_id` y, si la observación nace desde una tarjeta, `supplier_id`,
`offer_snapshot_id` y `quote_id`. Cualquier campo extra (preferencias, sujetos, estados,
organización) es `400 VALIDATION_ERROR`. Texto vacío: `400`. Solo espacios: `422
OBSERVATION_INVALID`. Más de 2000 caracteres: `400`. Artículo de otra organización o inexistente:
`404`.

Con `Idempotency-Key`, repetir la misma clave y el mismo `trade_item_id` + texto devuelve la
observación original (`201`, misma `observation_id`), ya con su estado interpretado si lo tenía.
La misma clave con otro contenido responde `409 IDEMPOTENCY_KEY_REUSED`.

### 2.2 Interpretar

```http
POST /api/v1/commercial-observations/0b7e…/interpretations
```

`200 OK`:

```json
{
  "replayed": false,
  "observation": {
    "observation_id": "0b7e…",
    "interpretation_status": "suggested",
    "interpretation": {
      "interpretation_id": "c4a2…",
      "status": "suggested",
      "error_code": null,
      "retired_subject_kinds": [],
      "created_at": "2026-11-03T18:04:26Z",
      "preferences": [
        {
          "preference_id": "9e11…",
          "subject_kind": "laboratory",
          "subject_label": "Laboratorio Chile",
          "effect": "prefer",
          "tolerance": null,
          "price_reference": null
        }
      ]
    },
    "interpretations": [ … ],
    "retired_subject_kinds": []
  }
}
```

- `replayed: true` significa que **no** hubo llamada nueva: se devolvió el último intento ya
  procesado. Un cliente que reintenta no duplica interpretaciones ni consume cuota.
- Solo un intento `failed` puede reintentarse y crear uno nuevo.
- `503 OBSERVATION_AI_DISABLED` si la instalación no tiene proveedor configurado. La observación
  permanece intacta.
- Un fallo del proveedor no lanza 5xx: persiste un intento `failed` con `error_code` cerrado
  (`timeout`, `provider_error`, …) y las preferencias anteriores siguen vigentes.

### 2.3 Historial

```http
GET /api/v1/commercial-observations?trade_item_id=6d1f…&limit=20
GET /api/v1/commercial-observations?trade_item_id=6d1f…&limit=20&cursor=MjAyNi0xMS0wM1QxODowNDoyMVp8MGI3ZQ
```

```json
{ "observations": [ … ], "next_cursor": "MjAyNi0xMS0wM1QxODowNDoyMVp8MGI3ZQ" }
```

Más recientes primero. `next_cursor: null` marca el final. Mismo patrón que E10. En el listado
`interpretations` contiene como máximo el intento más reciente; el detalle contiene todos.

### 2.4 Preferencias vigentes

```http
GET /api/v1/articles/6d1f…/preferences
```

```json
{
  "article_id": "6d1f…",
  "preferences": [
    { "preference_id": "9e11…", "subject_kind": "laboratory", "subject_label": "Laboratorio Chile",
      "effect": "prefer", "tolerance": null, "price_reference": null }
  ],
  "retired_subject_kinds": [],
  "history": [
    { "preference_id": "9e11…", "subject_kind": "laboratory", "subject_label": "Laboratorio Chile",
      "effect": "prefer", "tolerance": null, "price_reference": null,
      "interpretation_id": "c4a2…", "subject_class": "laboratory", "status": "suggested",
      "is_active": true, "created_at": "2026-11-03T18:04:26Z" }
  ],
  "retirements": [
    { "subject_kind": "laboratory", "interpretation_id": "d5b3…", "observation_id": "1c9a…",
      "created_at": "2026-11-03T18:10:02Z", "is_active": true }
  ]
}
```

- `preferences` es lo que el motor determinista aplica hoy (precedencia temporal resuelta).
- `history` incluye las reemplazadas (`is_active: false`); nada se borra.
- `retirements` son retiros explícitos por clase (D-144); `is_active` indica si siguen rigiendo.
- Solo lee datos persistidos. No interpreta, no llama a OpenAI y no reordena ofertas.

## 3. Flujo completo

1. El admin elige el artículo y escribe el texto libre.
2. `POST /commercial-observations` guarda el texto original (`text_original`) y devuelve
   `observation_id` con `interpretation_status = "sin_interpretacion"`.
3. El frontend, en la misma acción de guardar, llama `POST
   /commercial-observations/{id}/interpretations`.
4. El backend valida la salida del modelo, aplica o retira preferencias y guarda el intento.
5. El frontend pinta el resultado con la respuesta del paso 3 (o vuelve a consultar detalle,
   historial y preferencias).

Reintentos: repetir el paso 3 es seguro. Repetir el paso 2 con la misma `Idempotency-Key` es
seguro. Sin clave, cada guardado crea una observación nueva: el frontend debe enviarla.

## 4. Estados que verá el frontend

| `interpretation_status` | Significado | Efecto en preferencias |
| --- | --- | --- |
| `sin_interpretacion` | Guardada, aún no interpretada | Ninguno |
| `suggested` | Interpretación válida aplicada | Establece/reemplaza; respeta retiros |
| `no_preference` | No había preferencia que extraer | Ninguno |
| `needs_review` | Ambigüedad o caso dudoso | Ninguno; queda para revisión humana |
| `failed` | El proveedor falló o la salida fue inválida | Ninguno; se puede reintentar |
| `rejected` | Revisión humana la rechazó (no expuesto aún por API) | Ninguno |
| `confirmed` | Revisión humana la confirmó (no expuesto aún por API) | Igual que `suggested` |

`error_code` es un código cerrado y seguro para mostrar (`timeout`, `ambiguous`,
`provider_error`, `invalid_output`, …), nunca el prompt, la salida cruda ni el razonamiento.

## 5. Controles de seguridad

- `StrongActor` en todas las rutas; los servicios exigen `admin`. Sin rol: `403 FORBIDDEN`.
- La organización sale siempre del `Actor`; nunca del cuerpo ni de la URL.
- Artículo y observación se buscan por organización. Un id ajeno responde `404`, igual que uno
  inexistente; no se filtra su existencia.
- `get_observation` une la observación con su artículo y exige que ambos sean de la organización.
- Cuerpos con `extra="forbid"`: el cliente no puede inyectar preferencias, sujetos validados,
  estados, organización ni resultados de IA.
- Las lecturas no exponen `raw_output`, prompts, tokens, latencia ni confianza.
- No hay colas, workers ni WebSockets: la interpretación ocurre en la misma petición.

## 6. Idempotencia y concurrencia

- **Observaciones:** `Idempotency-Key` se registra en el `AuditEvent` de creación junto con un
  hash `sha256(trade_item_id|text)`, bajo un `advisory lock` por organización y clave. No requiere
  columnas ni migración.
- **Interpretaciones:** se consulta el último intento de la observación dentro de la transacción.
  Un intento aplicado, `no_preference` o `needs_review` se devuelve tal cual (`replayed: true`).
  Solo `failed` permite un intento nuevo; las solicitudes concurrentes quedan serializadas por la
  transacción y la segunda reutiliza lo persistido.

## 7. Migraciones

**Ninguna.** Fase 1 (`observation_interpretation`, `commercial_preference`) y D-144
(`retired_subject_kinds`, `0019`) ya aportan el esquema. La idempotencia usa `audit_event`, y la
paginación usa `created_at` + `id` de `commercial_observation`.

## 8. Uso desde Lovable

«Enseñar a la IA» guarda la observación y solicita su interpretación por la API. La responsable del
proyecto confirmó que esta función opera en el sitio publicado. Los resultados por versión están en el
[informe de la comparación con OpenAI](../prompts/comparacion/resultados-2026-11-13.md).

## 9. Pruebas

`tests/test_observation_ai_api.py` (13 casos, PostgreSQL real en `farmacia_test`, proveedor
simulado, sin llamadas facturables): registro y lectura, rechazo sin permisos, aislamiento por
organización, texto vacío/solo espacios/largo/campos extra, idempotencia, interpretación
exitosa, reintento sin duplicados, ambigüedad sin cambio de preferencias, fallo del proveedor sin
pérdida de datos, retiro explícito, historial paginado y detalle, aislamiento por artículo y
proveedor no configurado (`503`).
