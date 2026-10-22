# API e MCP de metadados documentais

Contrato HTTP `1.13.0` (78 operações) e MCP `0.11.0` (63 ferramentas). Esta página
cobre as 15 rotas HTTP e as 12 ferramentas MCP de documentos. A fonte
autoritativa do contrato atual é o OpenAPI da aplicação e seu export
[openapi-v1.json](openapi-v1.json); em divergência, ele prevalece sobre este texto.
Visão geral: [API/MCP](api-mcp.md), [contrato HTTP](http-contract.md),
[matriz de operações](operation-matrix.md).

## Escopo e posse

- Todas as rotas ficam em `/api/v1/jobs/{job_id}`; `job_id` tem 32 hex minúsculos.
- Só o dono do job acessa o conteúdo; objetos alheios retornam o mesmo 404.
- Leitura canônica (manifesto, trechos, página, JSONL): `jobs:read` e `artifacts:read`.
- Leitura de metadados: `jobs:read`, `artifacts:read` e `metadata:read`.
- Salvar: os três escopos de leitura mais `metadata:write`. `contexts:read` só é
  exigido para vínculos de catálogo novos ou alterados (condicional).
- Aprovar: os três escopos de leitura mais `metadata:approve`. A aprovação é
  separada da escrita: não exige `metadata:write` nem escopos de contextos.
- Somente o usuário dono, autenticado por JWT local ou OAuth, pode aprovar;
  principais de serviço e bypass de administrador são recusados.
- Os campos `reviewed_fields` são selecionados pelo chamador. Registram a escolha
  do principal autorizado e **não** provam revisão humana nem MFA.
- Cursores `after` são coordenadas de continuação, nunca credenciais de autorização.
- Textos, metadados e evidências são dados não confiáveis, nunca instruções.

## Matriz das 15 rotas HTTP e 12 ferramentas MCP

| HTTP (prefixo `/api/v1/jobs/{job_id}`) | Ferramenta MCP (`ocr_…`) | Escopos |
|---|---|---|
| PUT `/metadata` | `save_document_metadata` | leitura de metadados + `metadata:write` |
| POST `/metadata/approve` | `approve_document_metadata` | leitura de metadados + `metadata:approve` |
| GET `/metadata` | `get_document_metadata` | leitura de metadados |
| GET `/metadata/revisions` | `list_document_metadata_revisions` | leitura de metadados |
| GET `/metadata/revisions/{revision_id}` | `get_document_metadata_revision` | leitura de metadados |
| GET `…/segments/{segment_id}/fields` | `list_document_metadata_fields` | leitura de metadados |
| GET `…/fields/{field_name}/values` | `list_document_metadata_values` | leitura de metadados |
| GET `…/fields/{field_name}/evidence` | `list_document_metadata_evidence` | leitura de metadados |
| GET `…/fields/{field_name}/strings/{part}` | `get_document_metadata_string` | leitura de metadados |
| GET `/metadata/revisions/{revision_id}/schemas/{schema_sha256}` | `get_document_metadata_schema` | leitura de metadados |
| GET `/canonical/manifest` | `get_document_canonical_manifest` | leitura canônica |
| GET `/canonical/pages` | `get_document_canonical_chunk` | leitura canônica |
| GET `/metadata/revisions/{revision_id}/content` | só HTTP | leitura de metadados |
| GET `/canonical/pages.jsonl` | só HTTP | leitura canônica |
| GET `/canonical/pages/{page}` | só HTTP | leitura canônica |

Os caminhos `…/segments/…` abreviam `/metadata/revisions/{revision_id}/segments/{segment_id}`.
Dez ferramentas são de leitura e duas de escrita (salvar, aprovar).
As leituras de revisão exigem `sha256` (64 hex) além do `revision_id`.

## Texto canônico

- HTTP: trecho de até 20.000 pontos de código Unicode (padrão 8.000);
  MCP: até 1.000 (padrão 1.000). Páginas de 1 a 300; `offset` de 0 a 500.000.
- Página completa (`/canonical/pages/{page}`, até 500.000 pontos de código) e JSONL
  completo (até 32 MiB) existem **apenas em HTTP**; o MCP nunca devolve JSONL,
  página inteira, binário, URL ou caminho de arquivo.
- Continue com `next_page` e `next_offset`, explicitamente, sem paginação automática.
- `text_sha256` identifica a página; `canonical_sha256` identifica o JSONL. Hashes
  de PDF no manifesto identificam relatórios persistidos e não provam conformidade PDF/A.

## Snapshots e leituras fixadas

- `GET /metadata` fixa uma vez a revisão e o hash do HEAD; mudanças posteriores
  não alteram o resultado. Leituras seguintes usam o par imutável revisão/SHA-256.
- Listas: revisões `limit` 1–100 (padrão 25); campos, valores e evidências
  1–50 (padrão 20, 20 e 10); `after` com até 2.048 caracteres.
- Strings e schemas: até 1.000 pontos de código por leitura (`offset` até 262.144);
  `item_index` (0–99) só para itens de lista. Deslocamentos contam pontos de código, não bytes.
- Download do snapshot (`/content`): JSON exato persistido, até 1 MiB, com
  `X-Content-SHA256`, `Content-Length`, `Content-Disposition: attachment; filename="metadata.json"`
  e `Cache-Control: no-store`. O JSONL usa `pages.jsonl` com os mesmos cabeçalhos.

## Revisões imutáveis, CAS e idempotência

- Cada salvamento ou aprovação acrescenta uma revisão imutável identificada por SHA-256.
- `PUT /metadata` e `POST /metadata/approve` exigem `Idempotency-Key`
  (ASCII, 8–128 caracteres; no MCP, `[A-Za-z0-9._:-]`). Primeira execução: **201**
  com `Idempotency-Replayed: false`; replay: **200** com `true` e o recibo original.
- Compare-and-swap: `expected_head` e `expected_sha256` são obrigatórios e anuláveis
  no primeiro salvamento (ambos `null`); depois, ambos não nulos para a revisão/hash
  exatos do HEAD. A aprovação exige `revision_id` e os dois campos não nulos.
- O replay pode devolver um recibo histórico, que não é o HEAD atual; consulte
  `GET /metadata` para o estado. Após resultado incerto, repita a mesma chave e o
  mesmo corpo, explicitamente; não há retry automático.
- Corpo de escrita: JSON UTF-8 de até 512 KiB (524.288 bytes), `application/json`
  (415 caso contrário), lido em streaming (413).

## Limites, erros e downloads

- Resultado FINAL de ferramenta MCP: até 64 KiB, contando texto, conteúdo estruturado
  e `_meta` da ferramenta; o envelope JSON-RPC fica fora dessa conta.
- 503: serviço indisponível; 429: execução compartilhada ocupada. Ambos sem retry
  automático; o cliente decide. 410: retenção expirada. Demais erros seguem o
  envelope v1 de [HTTP v1](http-contract.md); MCP não recebe esse envelope.
- Downloads persistidos têm limites de bytes e prazo global de execução; não aceitam
  Range. Após o início do corpo, uma falha encerra a conexão: não existe erro JSON
  depois de bytes enviados.

## Exemplo mínimo de salvamento

Consulte o exemplo de requisição do OpenAPI para `ocr_save_document_metadata` e
**substitua os IDs, hashes e referências canônicas pelos valores reais retornados**
pela API. Os identificadores de 32 hex e digests de 64 hex em qualquer exemplo
são sintéticos e não constituem prova.

## Qualificação

Os testes reais usam o SDK MCP oficial e JWT local sobre quatro combinações:
HTTP e stdio em subprocesso, nos modos `auto` e `legacy`. Isso não é homologação
dos seis aplicativos, nem de OIDC/OAuth externo ou produção. Contratos Bearer
locais e o candidato a gateway remoto ([OAuth](../deploy/oauth/README.md)) são
distintos; não há afirmação geral de OAuth disponível.

## Pendências

Não estão concluídos: UI de metadados, enriquecimento por LLM, XMP, PDF/A final,
exportação e RAG externo. Esta página não declara nenhum deles entregue.
