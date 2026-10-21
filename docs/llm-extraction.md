# Extração assistida por LLM (OCR-EV-03 / E3)

A extração assistida propõe valores de metadados, ou o tipo documental, a partir do texto canônico do OCR. Ela **nunca altera metadados sozinha**. O resultado é uma *execução* (`ocr-extraction-run/1`) com propostas e evidências. Aplicar uma execução cria um rascunho comum (`origin=extracted`), que continua exigindo revisão explícita e aprovação por um usuário (`ocr_approve_document_metadata`). O OCR, o PDF/A e os pacotes funcionam com a extração desligada ou indisponível.

## Decisões do owner (21/10/2026)

- **Modelo local.** O texto de documentos reais só vai para o runtime de modelo do próprio owner, nunca para provedores externos.
- **Rota.** O Ollama do host `andrellm` (RTX 3060, `qwen3:14b`) é acessado pelo Cloudflare Tunnel + Access, o mesmo caminho do LicitaFlow, com um service token próprio do OCR. O owner considera essa rota dentro da política.
- **Desligada por padrão.** `EXTRACTION_ENABLED=false`. Enquanto estiver desligada, toda chamada retorna `503 extraction_disabled`.

## Configuração

| Variável | Padrão | Uso |
|---|---|---|
| `EXTRACTION_ENABLED` | `false` | Liga a funcionalidade e funciona como chave de desligamento rápido |
| `EXTRACTION_BASE_URL` | — | Endpoint compatível com OpenAI, por exemplo `https://llm.exemplo/v1` (`/chat/completions`) |
| `EXTRACTION_MODEL` | — | Nome do modelo no runtime |
| `EXTRACTION_ALLOWED_HOSTS` | vazio | Hosts aceitos quando **todos** os endereços resolvidos são loopback, privados (RFC 1918/ULA) ou Tailscale (100.64/10). A verificação é refeita antes de cada chamada |
| `EXTRACTION_ACCESS_HOSTS` | vazio | Hostnames publicados pelo Cloudflare Tunnel + Access do owner. Exigem HTTPS e service token |
| `EXTRACTION_ACCESS_TOKEN_FILE` | — | Arquivo JSON `{"client_id","client_secret"}` lido só no servidor, nunca de variável de ambiente, argv ou log |
| `EXTRACTION_API_KEY_FILE` | — | Chave Bearer opcional do runtime |
| `EXTRACTION_CALL_TIMEOUT_SECONDS` | 120 | Prazo por chamada |
| `EXTRACTION_RUN_TIMEOUT_SECONDS` | 600 | Prazo total de uma execução (`run_timeout`) |
| `EXTRACTION_MAX_CHUNK_CHARS` | 12000 | Caracteres por chamada |
| `EXTRACTION_MAX_CALLS_PER_RUN` | 6 | Chamadas por execução. Acima disso, `413 document_too_large` (sem truncar em silêncio) |
| `EXTRACTION_MAX_OUTPUT_TOKENS` | 1500 | Tokens de saída por chamada. Resposta cortada vira `output_truncated` |
| `EXTRACTION_OWNER_DAILY_TOKENS` | 400000 | Orçamento diário (UTC) de tokens por dono, conferido antes de cada chamada |
| `EXTRACTION_MAX_RUNS_PER_JOB` | 20 | Execuções por trabalho |
| `EXTRACTION_MAX_QUEUED` | 8 | Execuções aguardando ou em andamento no servidor |

Qualquer endpoint fora dessas duas classes, inclusive IP público, link-local, redirecionamento ou URL com credenciais, resulta em `provider_policy` antes de qualquer envio. Num endpoint local em HTTP, a conexão vai ao endereço já validado, com o `Host` original, e o nome não é resolvido de novo; assim, uma resposta DNS alterada depois da verificação não desvia o texto. Em HTTPS o nome é mantido, porque o certificado já amarra a conexão a ele.

## Escopos

| Operação | Escopos |
|---|---|
| Listar e ler execuções | `jobs:read`, `artifacts:read`, `metadata:read` |
| Criar execução (consome o orçamento do modelo e lê o catálogo) | + `metadata:write`, `contexts:read` |
| Aplicar execução (cria rascunho) | + `metadata:write`; `contexts:read` só quando cria um segmento novo |

Sempre se exige o mesmo dono do trabalho, sem bypass de administrador. Trabalho de outro dono responde `404`.

## Fluxo

1. `POST /api/v1/jobs/{job_id}/extractions` (MCP `ocr_create_metadata_extraction`) com **um** destes campos:
   - `type_id`, e opcionalmente `type_version`: propõe valores para os campos do tipo publicado;
   - `context_id`: propõe qual tipo publicado do contexto o documento é (classificação).

   Os campos `page_start` e `page_end` limitam as páginas. A requisição lê as páginas canônicas com a identidade do chamador, confere os limites e enfileira a execução (`202`). Entradas idênticas (dono, texto, schema, páginas, modelo, versão do prompt e parâmetros) devolvem a execução existente (`200`, `replayed=true`). `retry=true` cria uma nova execução, porque reexecutar um LLM não garante a mesma saída.
2. Um único worker por processo chama o modelo com temperatura 0 e resposta em JSON.
3. `GET .../extractions/{run_id}` (`ocr_get_metadata_extraction`) mostra:
   - o estado (`queued`, `running`, `completed`, `failed`) e o `outcome`;
   - o modelo, a versão do prompt e o consumo (chamadas, tokens, latência);
   - as propostas, os candidatos em conflito e as contagens de rejeição.
4. `POST .../extractions/{run_id}/apply` (`ocr_apply_metadata_extraction`) recebe `expected_head`/`expected_sha256` (CAS) e `Idempotency-Key`, e cria um rascunho:
   - **valores existentes nunca são sobrescritos**: só campos sem valor recebem propostas, com `origin=extracted` e `provenance.execution_id` igual à execução;
   - os demais segmentos e campos são mantidos como estão;
   - páginas sobrepostas a um segmento de outro tipo ou intervalo resultam em `409 extraction_segment_conflict`;
   - texto canônico alterado depois da execução resulta em `409 extraction_source_changed`.
5. A aprovação continua sendo feita só por `ocr_approve_document_metadata`, com `reviewed_fields` explícitos. Campos críticos e campos com `evidence_required` precisam de evidência literal.

## Verificação das propostas

A resposta do modelo é tratada como dado não confiável. Para cada valor proposto:

- o envelope JSON é fechado; um item malformado é descartado e contado (`malformed_value`), sem derrubar a resposta inteira;
- o nome precisa existir no schema publicado (`unknown_field`), e o valor precisa passar pela normalização do `FieldSpec`:
  - texto decimal no formato brasileiro (`1.234,56`) e números JSON são convertidos para decimal com ponto antes da normalização;
  - números JSON fora de campos decimais são recusados (`invalid_value`);
- a citação precisa existir literalmente na página citada, ou em outra página do mesmo bloco. Os offsets são **recalculados** no texto canônico, e a citação gravada é sempre a fatia exata (`quote_not_found`);
- uma citação tirada de uma linha que se dirige ao modelo ("ignore as instruções", "SYSTEM:", "responda…") nunca vale como evidência (`suspicious_source`);
- o valor precisa estar visível na citação: texto contido (sem diferenciar maiúsculas e acentos), dígitos de números e decimais, e dia/mês/ano de datas, inclusive mês por extenso (`value_not_in_quote`). Enum e booleano exigem apenas a citação;
- o texto do documento vai ao modelo cercado por uma tag com nonce aleatório, e o prompt manda ignorar instruções contidas nele. A defesa principal, porém, é a verificação acima somada à revisão humana.

Resultado por campo:

| Caso | Situação |
|---|---|
| Nenhum valor verificado | `not_found` |
| Um valor (o mesmo valor citado em vários lugares acumula até 3 evidências) | `needs_review` |
| Valores diferentes num campo de valor único | `conflict` |
| Campo de valores múltiplos (`many`) | Um item por valor, com evidência por `item_index`; o excedente de `max_items` é contado |

Um conflito é aplicado **sem valor** (os candidatos ficam na execução) e gera o bloqueio `conflict_unresolved`. Para aprovar, uma pessoa grava a escolha com `ocr_save_document_metadata`.

Há também uma etapa determinística: num campo enum, se exatamente um valor permitido aparece literalmente no texto, ele é proposto com `methods=["deterministic"]`. Quando o modelo concorda, `methods` lista os dois métodos.

## Persistência, logs e falhas

- A tabela `extraction_runs` (no banco de trabalhos) guarda a identidade, os hashes do texto e do schema, o modelo, a versão do prompt, o consumo, as propostas e as citações curtas. Prompts, respostas do modelo e texto das páginas **não** são gravados nem registrados em log. O log mostra só o ID da execução, o `outcome`, as chamadas e os tokens.
- Códigos de falha (`state=failed`):
  - `invalid_output`, `output_truncated`;
  - `provider_timeout`, `provider_unavailable`, `provider_quota`, `provider_error`, `provider_policy`;
  - `run_timeout`, `budget_exceeded`;
  - `interrupted`: execução pendente quando o processo reiniciou, porque a fila vive só em memória.
- `abstained` é uma conclusão normal, sem nenhum valor verificado.

## Avaliação com corpus sintético

O arquivo `tests/fixtures/extraction_corpus.json` traz 9 documentos inventados (contratos e matrículas), com injeções, conflitos, ruído de OCR e valores ausentes. O script `scripts/extraction_eval.py` mede, por campo e por divisão, precisão, revocação, abstenções corretas, conflitos, rejeições, latência e tokens.

Em 21/10/2026, com `qwen2.5:7b-instruct` local, só CPU/GPU de estação e os 9 documentos: precisão 0,94 e revocação 0,94 (46 corretos, 3 errados, 3 omitidos e 8 abstenções corretas), com cerca de 30 s por documento. As regras de injeção e de decimal foram ajustadas depois de ver os erros do corpus inteiro, então esse número **não** é uma avaliação independente. Antes de ativar em produção:

- ampliar o corpus de avaliação separado;
- medir com o `qwen3:14b` do andrellm;
- fixar limites mínimos de qualidade por campo.

## Fora do escopo

- Ativação em produção, que depende do service token do OCR no Cloudflare Access e dos limites de qualidade por campo.
- Editor ou aprovação gráfica, por ordem do owner de 20/10/2026.
- Coordenadas na página.
- Extração de imagens sem texto.
- Cache entre donos diferentes, que nunca é compartilhado.
