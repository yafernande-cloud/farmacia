# Ejemplos de listas de proveedores

`lista_distribuidora_andes.xlsx` es una planilla de demostración **ficticia** de «Distribuidora Andes».
Los productos, precios, stocks, laboratorios y marcas son inventados para probar la importación; no
representan acuerdos comerciales reales.

Cubre, en la hoja «Lista Andes»:

| Filas | Caso |
|---|---|
| AND-001 a AND-008 | Laboratorio en columna propia; marca no informada |
| AND-009 a AND-014 | Marca en columna propia; laboratorio no informado |
| AND-015 a AND-020 | Laboratorio y marca en columnas propias |
| AND-021 a AND-026 | Sin columnas de identidad; el nombre menciona laboratorio, marca o ambos con «LAB.»/«LABORATORIOS»/«MARCA» |
| AND-027 a AND-030 | Casos ambiguos: un nombre suelto («ANDES»), una «línea», «S/M» y «LAB/MARCA» |
| AND-006 y AND-020 | Informan condiciones comerciales (revisión distinta de la identidad) |

Cómo se leen las filas de identidad (D-150):

- **AND-001 a AND-020** publican laboratorio y/o marca en columnas propias. Se guardan tal cual y
  `evidence.identity.status` queda `published` (14 con laboratorio, 12 con marca).
- **AND-021 a AND-026** traen el dato solo dentro de la descripción. Cada nombre usado ahí aparece
  también en una columna de otra fila del mismo archivo, así que el **vocabulario controlado** del
  archivo permite reconocerlo con una marca explícita y guardarlo con procedencia `description`
  (`status = extracted`). Resultado: 4 laboratorios y 3 marcas reconocidos. La descripción original
  no se modifica.
- **AND-027 a AND-030** no tienen marca explícita o su texto es ambiguo («ANDES», «LINEA
  CORDILLERA», «S/M», «LAB/MARCA SUR»). No se reconoce nada: quedan `status = unconfirmed`, sin
  laboratorio ni marca, y la pantalla los muestra como pendientes de identificación.

Ningún nombre se inventa: si no está confirmado en una columna del archivo, en el catálogo propio
o en una lista anterior, la descripción no basta.

No se debe importar en una base real ni de demostración sin autorización explícita. El archivo
original de la demostración anterior no estaba en el repositorio; si aparece, se conserva sin
modificar como respaldo.
