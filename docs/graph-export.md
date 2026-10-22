# Exportação para grafo: `ocr-graph/1` e entrega `ocr-rag/2` (OCR-EV-06)

O OCR exporta entidades, relações e alegações (claims) de um documento a partir de **um snapshot de metadados aprovado**. Cada relação ou alegação leva a evidência de página que foi revisada para aquele valor.

O OCR não monta um índice GraphRAG. Comunidades, embeddings e consultas ficam no sistema consumidor, e a identidade das entidades é resolvida de forma conservadora lá também. O contrato está em `app/graph_contract.py`, cópia byte a byte do arquivo do receptor independente (`document-rag`).

## Onde obter

| Canal | Operação | Escopos |
| --- | --- | --- |
| HTTP | `GET /api/v1/jobs/{job_id}/graph?metadata_revision_id=&sensitivity_ceiling=` | `jobs:read`, `artifacts:read`, `metadata:read` |
| MCP | `ocr_get_document_graph` | os mesmos |
| Entrega RAG | `POST /api/v1/rag/deliveries` com `include_graph=true` (ou `ocr_submit_rag_delivery`) | os da entrega, mais `metadata:read` |

- **Revisão usada:** sem `metadata_revision_id`, vale a revisão aprovada mais recente. Um rascunho ou uma revisão desconhecida respondem `409 metadata_not_approved`.
- **Teto padrão:** o teto (`sensitivity_ceiling`) é `internal`.
- **Limite do MCP:** o MCP devolve no máximo 64 KiB por resposta. Um grafo maior responde `response_too_large` e precisa ser lido por HTTP.

## O que entra

- **Valores incluídos:** só valores `reviewed` de campos com `indexable=true` no tipo publicado e com sensibilidade até o teto.
- **Valores fora:** o que fica de fora é apenas contado em `omitted` (`not_indexable`, `above_ceiling`, `not_reviewed`); o valor nunca aparece.
- **Opt-in:** `indexable` vale `false` por padrão, então incluir dados no grafo é uma decisão explícita de quem define o tipo documental.

**Nós:**
- um nó `document`;
- um nó `segment` por segmento, com tipo, versão e páginas;
- nós `entity` para valores de:
  - campos do tipo `identifier` (`entity_type=identifier`);
  - campos de texto com papel de entidade, listados em `ENTITY_FIELDS`: `emissor` e `partes` (agent), `entidade_responsavel` e `serventia` (organization), `municipio` (place).

**Relações:**
- `document → segment` (`has_segment`);
- `segment → entity` (`field:<nome>`), com evidência.

**Alegações:** os demais valores (datas, quantias, textos) viram alegações do segmento, com `value_type` e evidência.

## Identidade: nunca unir só pelo nome

- **Escopo da identidade:** a identidade de todo nó é limitada ao documento. Para entidades, ela inclui segmento, campo e item da lista.
- **IDs derivados:** o `node_id` é derivado (`sha256` da identidade), e o receptor o recalcula. Por isso dois valores com o mesmo texto, no mesmo documento ou em documentos diferentes, são sempre nós distintos.
- **Identificadores como sugestão:** valores de identificador também vão como `hints` (`scheme=identifier`, campo e valor). O consumidor pode usar essa sugestão para vincular entidades de forma explícita e auditável. Uma sugestão não é uma união.

## Evidência e verificação

Cada evidência traz:
- a página;
- `start`/`end` em code points Unicode;
- a citação (`quote`);
- o `text_sha256` do texto da página, que é o mesmo texto entregue no artefato `pages`.

`verify_graph` (no receptor) confere cada ponto abaixo e recusa a entrega se algum não bater:
- documento, hash do PDF e hash dos metadados;
- IDs derivados;
- página por página, o `sha256` e o trecho `texto[start:end] == quote`.

Passar na verificação mostra que o valor é rastreável ao snapshot aprovado e à página revisada. Não mostra que o valor é verdadeiro.

## Entrega `ocr-rag/2`

- **Pacote:** com `include_graph=true`, o pacote passa a ter `pdf`, `pages`, `metadata` (os bytes exatos do snapshot aprovado) e `graph`, com `metadata_state=approved`.
- **Versões:** `ocr-rag/2` só existe com grafo, e grafo só existe com metadados aprovados. Um receptor `ocr-rag/1` recusa o pacote em vez de descartar o grafo.
- **Recibo:** o recibo precisa repetir a versão do manifesto.
- **Entrega sem grafo:** continua exatamente como antes (`ocr-rag/1`, mesmo `delivery_id`).
- **Teto por destino:** cada destino configurado pode declarar `graph_sensitivity_ceiling` em `RAG_CONFIG_FILE` (padrão `internal`). O campo não entra no vínculo do destino, então as entregas já admitidas não mudam.
- **Uma entrega por trabalho:** a fila guarda uma entrega por trabalho. Um trabalho já entregue sem grafo devolve `409 rag_delivery_conflict` quando se pede o grafo. Para entregar o grafo depois, falta um contrato de substituição no destino, ainda não implementado. Aprove os metadados antes da primeira entrega.

## Limites

- **Mapeamento de entidades:** o mapeamento campo → entidade é fixo por nome de campo. Campos de outros nomes viram alegações.
- **Templates:** os templates do catálogo não marcam campos como `indexable`.
- **Escopo do grafo:** o grafo cobre só metadados revisados; não extrai entidades do texto livre.
- **Resolução de entidades:** a resolução entre documentos fica no consumidor e nunca é automática por nome.
