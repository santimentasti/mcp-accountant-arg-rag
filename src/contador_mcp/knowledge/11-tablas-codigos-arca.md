---
title: Tablas de códigos ARCA (factura electrónica)
fuente: Parámetros WSFEv1 de ARCA (FEParamGet*) vía django-afip
url: https://www.arca.gob.ar/ws/documentacion/manuales/manual-desarrollador-ARCA-COMPG-v4-0.pdf
impuesto: iva
jurisdiccion: nacional
tipo: norma
vigente_desde: 2025-01-01
---
# Tablas de códigos ARCA

## Tipos de comprobante

- Código 1: Factura A
- Código 2: Nota de Débito A
- Código 3: Nota de Crédito A
- Código 4: Recibos A
- Código 5: Notas de Venta al contado A
- Código 6: Factura B
- Código 7: Nota de Débito B
- Código 8: Nota de Crédito B
- Código 9: Recibos B
- Código 10: Notas de Venta al contado B
- Código 11: Factura C
- Código 12: Nota de Débito C
- Código 13: Nota de Crédito C
- Código 15: Recibo C
- Código 34: Cbtes. A del Anexo I, Apartado A,inc.f),R.G.Nro. 1415
- Código 35: Cbtes. B del Anexo I,Apartado A,inc. f),R.G. Nro. 1415
- Código 39: Otros comprobantes A que cumplan con R.G.Nro. 1415
- Código 40: Otros comprobantes B que cumplan con R.G.Nro. 1415
- Código 49: Comprobante de Compra de Bienes Usados a Consumidor Final
- Código 51: Factura A con Leyenda "Operación Sujeta a Retención"
- Código 52: Nota de Débito A con Leyenda "Operación Sujeta a Retención"
- Código 53: Nota de Crédito A con Leyenda "Operación Sujeta a Retención"
- Código 54: Recibo A con Leyenda "Operación Sujeta a Retención"
- Código 60: Cta de Vta y Liquido prod. A
- Código 61: Cta de Vta y Liquido prod. B
- Código 63: Liquidacion A
- Código 64: Liquidacion B
- Código 201: Factura de Crédito electrónica MiPyMEs (FCE) A
- Código 202: Nota de Débito electrónica MiPyMEs (FCE) A
- Código 203: Nota de Crédito electrónica MiPyMEs (FCE) A
- Código 206: Factura de Crédito electrónica MiPyMEs (FCE) B
- Código 207: Nota de Débito electrónica MiPyMEs (FCE) B
- Código 208: Nota de Crédito electrónica MiPyMEs (FCE) B
- Código 211: Factura de Crédito electrónica MiPyMEs (FCE) C
- Código 212: Nota de Débito electrónica MiPyMEs (FCE) C
- Código 213: Nota de Crédito electrónica MiPyMEs (FCE) C

## Condición frente al IVA del receptor (RG 5616)

Desde la RG 5616 la condición frente al IVA del receptor es un dato obligatorio del comprobante electrónico. La columna indica qué clases de comprobante se pueden emitir a cada condición.

- Código 1: IVA Responsable Inscripto (clases: A/ALEY/C)
- Código 4: IVA Sujeto Exento (clases: B/C)
- Código 5: Consumidor Final (clases: C/49)
- Código 6: Responsable Monotributo (clases: A/ALEY/C)
- Código 7: Sujeto No Categorizado (clases: B/C)
- Código 8: Proveedor del Exterior (clases: B/C)
- Código 9: Cliente del Exterior (clases: B/C)
- Código 10: IVA Liberado – Ley N° 19.640 (clases: B/C)
- Código 13: Monotributista Social (clases: A/ALEY/C)
- Código 15: IVA No Alcanzado (clases: B/C)
- Código 16: Monotributo Trabajador Independiente Promovido (clases: A/ALEY/C)

## Tipos de documento

- Código 0: CI Policía Federal
- Código 1: CI Buenos Aires
- Código 2: CI Catamarca
- Código 3: CI Córdoba
- Código 4: CI Corrientes
- Código 5: CI Entre Ríos
- Código 6: CI Jujuy
- Código 7: CI Mendoza
- Código 8: CI La Rioja
- Código 9: CI Salta
- Código 10: CI San Juan
- Código 11: CI San Luis
- Código 12: CI Santa Fe
- Código 13: CI Santiago del Estero
- Código 14: CI Tucumán
- Código 16: CI Chaco
- Código 17: CI Chubut
- Código 18: CI Formosa
- Código 19: CI Misiones
- Código 20: CI Neuquén
- Código 21: CI La Pampa
- Código 22: CI Río Negro
- Código 23: CI Santa Cruz
- Código 24: CI Tierra del Fuego
- Código 80: CUIT
- Código 86: CUIL
- Código 87: CDI
- Código 89: LE
- Código 90: LC
- Código 91: CI Extranjera
- Código 92: en trámite
- Código 93: Acta Nacimiento
- Código 94: Pasaporte
- Código 95: CI Bs. As. RNP
- Código 96: DNI
- Código 99: Doc. (Otro)

## Alícuotas de IVA (códigos)

- Código 3: 0%
- Código 4: 10.5%
- Código 5: 21%
- Código 6: 27%
- Código 8: 5%
- Código 9: 2.5%

## Tipos de tributo

- Código 1: Impuestos nacionales
- Código 2: Impuestos provinciales
- Código 3: Impuestos municipales
- Código 4: Impuestos Internos
- Código 5: IIBB
- Código 6: Percepción de IVA
- Código 7: Percepción de IIBB
- Código 8: Percepciones por Impuestos Municipales
- Código 9: Otras Percepciones
- Código 13: Percepción de IVA a no Categorizado
- Código 99: Otro

## Conceptos

- Código 1: Producto
- Código 2: Servicios
- Código 3: Productos y Servicios
