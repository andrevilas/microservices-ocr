# Pacotes documentais com XMP (OCR-EV-04 / E4a)

Um pacote é a forma local e interoperável de entregar um documento processado: PDF/A-2b com XMP, metadados aprovados, texto canônico, validação e manifesto. Não depende de LLM e não envia nada a destino externo (isso é E4b).

## Pré-condições

- Trabalho concluído com `output_format=pdfa` e `pdfa_conformance=passed`; outros resultados recebem `409 package_requires_pdfa`.
- Um snapshot de metadados **aprovado**. Sem `metadata_revision_id`, usa-se o aprovado mais recente; rascunho ou revisão desconhecida → `409 metadata_not_approved`.
- Original assinado: se o PDF de entrada tem assinatura digital (campo `/Sig` com valor) ou certificação DocMDP, ou se a estrutura não pôde ser inspecionada, `include_original=false` → `409 signed_original_required`. O OCR sempre reescreve o arquivo, então a assinatura nunca passa para `document.pdf`; o que se preserva são os bytes do original, idênticos e conferidos pelo hash, em `original.pdf`. A detecção é estrutural e não verifica a validade da assinatura.
- As fontes precisam corresponder ao snapshot: hash do PDF do OCR (relatório, validação anterior e snapshot), hash do original e hash do `pages.jsonl`. Qualquer divergência → `409 package_source_mismatch`, antes de escrever o PDF.

## Ordem de construção (fixa)

1. Copiar `metadata.json` (bytes literais do snapshot, conferidos pelo hash da revisão) e `pages.jsonl` (canônico, conferido pelo hash).
2. Projetar os campos do snapshot no XMP e gravar `document.pdf` com pikepdf. É a **última escrita** no PDF.
3. Reler o XMP gravado e comparar com a projeção e com o schema declarado. Diferença ou XMP inválido → `422 package_xmp_inconsistent` ou `package_xmp_invalid`, sem validar.
4. Validar `document.pdf` com veraPDF, perfil PDF/A-2b, sobre os bytes exatos. O hash é medido antes, informado pelo validador e medido depois; os três precisam coincidir. Qualquer veredito diferente de `passed`, validador não configurado, timeout, plataforma sem suporte ou bytes alterados → `422 package_pdfa_validation_failed` e nenhum pacote é publicado.
5. Gravar `validation.json` e `manifest.json` e montar `package.zip` (sem compressão). O hash do PDF final fica **somente** no manifesto e em `validation.json`, nunca dentro do PDF.

O timeout do veraPDF no pacote é `min(VERAPDF_TIMEOUT_SECONDS, 90)`, abaixo do prazo de 120 s da execução documental compartilhada.

## XMP

- O XMP existente do OCRmyPDF (`pdfaid`, datas, produtor etc.) é preservado; o pacote acrescenta duas `rdf:Description`.
- Namespace próprio e fixo `https://ocr.andre.goiania.br/ns/ocrdoc/1.0/` (prefixo `ocrdoc`), declarado em `pdfaExtension:schemas` como exige o PDF/A-2. Propriedades: `documentId`, `packageVersion`, `metadataRevision`, `metadataSha256`, `originalSha256`, `canonicalSha256` e `fields` (`bag Field`).
- O tipo `Field` (`https://ocr.andre.goiania.br/ns/ocrfield/1.0/`, prefixo `ocrfield`) tem `segment`, `name`, `index` e `value`. Como o schema é fixo, o veraPDF vê a mesma declaração para qualquer tipo documental; os campos de domínio viajam como valores, não como propriedades novas.
- Projeção: somente campos cujo `FieldSpec` publicado tem `destination="xmp"`, na ordem do schema, usando o valor `normalized`. Listas geram um item por posição (`index` 0, 1, …). Booleanos viram `true`/`false`.
- Campos `sensitivity="restricted"` **nunca** entram no PDF, mesmo com `destination="xmp"`; campos sem valor também ficam de fora. Os dois casos aparecem em `manifest.xmp.excluded` com o motivo (`restricted` ou `no_value`).
- Campos `sidecar` ficam apenas em `metadata.json`. O JSON é a representação semântica completa; o XMP é uma projeção do mesmo snapshot.
- O stream de metadados é gravado sem filtro e com `fix_metadata_version=False`: o pikepdf, por padrão, substituiria em silêncio um XMP não reconhecido por um vazio.
- XMP de origem acima de 4 MiB, malformado ou com DTD/entidades → `422 package_xmp_invalid`.

## Conteúdo do pacote (`ocr-document-package/1`)

| Arquivo | Conteúdo |
|---|---|
| `document.pdf` | PDF/A-2b final com XMP, validado |
| `original.pdf` | Original enviado, byte a byte (omitido com `include_original=false`) |
| `metadata.json` | Snapshot aprovado `ocr-document-metadata/1`, bytes literais |
| `pages.jsonl` | Texto canônico `ocr-canonical-page/1` |
| `validation.json` | Veredito do veraPDF sobre `document.pdf` (`ocr-package-validation/1`) |
| `manifest.json` | Identidade, versão, expiração, linhagem, resumo XMP, validação e hash/tamanho de cada arquivo |

A linhagem tem dois hashes de PDF distintos: `ocr_output_pdf_sha256` (PDF do OCR sem o XMP do pacote, ao qual o snapshot e o `pages.jsonl` se vinculam) e `final_pdf_sha256` (o `document.pdf` do pacote). Também registra o original, o snapshot, os schemas, o texto canônico e o pacote anterior (`previous_package_id`).

## Versões, expiração e integridade

- Cada pacote recebe `package_version` crescente por trabalho e um diretório próprio em `packages/<package_id>/`. Pacotes nunca são sobrescritos; nova revisão aprovada gera nova versão ligada à anterior.
- A criação é idempotente por trabalho + revisão aprovada + `include_original`: se já existe pacote `ready`, a chamada devolve o mesmo pacote (`200`, `replayed=true`) sem nova validação. Por isso não há `Idempotency-Key`. Uma construção por trabalho de cada vez (`409 package_build_in_progress`); no máximo `PACKAGE_MAX_PER_JOB` versões (padrão 20).
- `expires_at = min(criação + PACKAGE_TTL_SECONDS, expiração dos artefatos do trabalho)`. Pacotes ficam dentro do diretório do trabalho e saem com ele na retenção. Após expirar, ou se os artefatos forem removidos, o estado é `expired` e o download retorna `410 package_expired`.
- Cada download recalcula o SHA-256 e o tamanho do `package.zip` antes de enviar o primeiro byte e de novo durante o envio. Divergência ou arquivo ausente marcam o pacote como `invalid` **de forma permanente** (`409 package_invalid`); restaurar os bytes não o revalida, e é preciso criar uma nova versão.
- Tamanho máximo do ZIP: `PACKAGE_MAX_TOTAL_MB` (padrão 512) → `413 package_too_large`.

## API e MCP

Todos exigem `jobs:read`, `artifacts:read` e `metadata:read`, com o mesmo dono do trabalho e sem bypass de administrador. Trabalho de outro dono → `404`.

| HTTP | MCP | Uso |
|---|---|---|
| `POST /api/v1/jobs/{job_id}/packages` | `ocr_create_document_package` | Cria (201) ou devolve o existente (200). Corpo estrito: `metadata_revision_id`, `include_original` |
| `GET /api/v1/jobs/{job_id}/packages` | `ocr_list_document_packages` | Versões, da mais antiga à mais recente |
| `GET /api/v1/jobs/{job_id}/packages/{package_id}` | `ocr_get_document_package` | Estado, expiração, hashes e manifesto |
| `GET /api/v1/jobs/{job_id}/packages/{package_id}/content` | — | Download do `package.zip` (`X-Content-SHA256`, `Cache-Control: no-store`, sem Range) |

O MCP não devolve binário: use `download_path` com a mesma identidade Bearer. As rotas estão na allowlist do acesso remoto OAuth (`app/remote.py`). Não há interface gráfica para criar pacotes nem para editar ou aprovar metadados (ordem do owner de 20/10/2026).

## Fora do escopo desta etapa

- Entrega a destino externo, recibos de destino e limpeza condicionada a recibo (E4b).
- Assinatura do manifesto. Os hashes só detectam alteração quando comparados com uma referência confiável, como este serviço ou um recibo do destino.
- Projeção para campos padrão (`dc:title` etc.) a partir de campos de domínio. Não há mapeamento publicado, então nada é inferido.
