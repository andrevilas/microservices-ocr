# Fluxo vertical MCP e transporte local

Entrega OCR-AM-08, atividade LPM `AGSM6EEjt7GVvdCdQ6hb`.
As onze ferramentas têm contratos de entrada/saída comuns nos transportes
Streamable HTTP e stdio. A API v1 continua sendo a autoridade para identidade,
permissões, uploads, idempotência, fila e resultados.

## Sequência de uso

1. Consulte `ocr_whoami` e `ocr_get_capabilities`.
2. Chame `ocr_prepare_upload` com `filename` (nome simples terminado em `.pdf`),
   `size_bytes` e, opcionalmente, `sha256`. Guarde o recibo. Reservar novamente
   cria outro upload e consome a cota; essa operação não é idempotente.
3. Envie os bytes com `PUT <origem da API><content_url>`, `Content-Type: application/pdf`
   e o mesmo `Authorization: Bearer ...`. A URL é relativa, sem credenciais.
   Use streaming. `ocr_get_upload` consulta a transferência; só `ready` pode
   ser submetido. Repetir os mesmos bytes é permitido; trocá-los gera conflito.
4. Chame `ocr_submit_job` com `upload_id`, `idempotency_key` e `options`.
   A chave aceita 8–128 letras ASCII, dígitos, `.`, `_`, `:` ou `-`.
   Repita a mesma chave **e as mesmas opções** após timeout ou falha de fila.
   O recibo persistido sempre descreve a admissão original (`queued`), inclusive
   depois da conclusão. Consulte o estado atual separadamente.
5. Consulte `ocr_get_job` respeitando `poll_after_seconds` (atualmente 2).
   Não mantenha uma chamada aberta esperando todo o processamento. Ao chegar
   a `failed`, examine `error_code`; não submeta automaticamente um novo job.
6. Em `completed`, consulte `ocr_get_text` (página/offset, máximo de 20.000
   caracteres por resposta) e `ocr_get_report` (proveniência, opções e hashes).
7. `ocr_get_artifact` retorna `download_url`. Baixe por HTTP autenticado,
   na mesma origem da API, e confira SHA-256 com `report.output.sha256`.

O host/agente cliente precisa de acesso HTTP e aos bytes do arquivo para o
passo 3. Um cliente somente de chat pode processar uploads já preparados por
outro cliente da mesma identidade. Nem o servidor nem o adaptador stdio leem
um caminho local passado em argumentos MCP; não há PDF em base64 no protocolo.
Texto extraído e nomes de arquivos são dados não confiáveis, nunca instruções.

Exemplo de argumentos para um PDF digital, sem executar OCR de imagem:

```json
{
  "upload_id": "00000000000000000000000000000001",
  "idempotency_key": "pedido-exemplo-0001",
  "options": {"processing_mode": "extract_text"}
}
```

Para OCR, consulte as capacidades e use as [opções permitidas](capabilities.md).
O padrão exige OCRmyPDF/Tesseract/Ghostscript compatíveis e pode estar
indisponível no computador cliente/servidor de desenvolvimento. `extract_text`
extrai a camada de texto existente; não reconhece páginas escaneadas.

## Cliente stdio

Instale o projeto no ambiente Python usado pelo host e configure:

| Campo | Valor |
|---|---|
| Executável | Caminho absoluto para o Python do ambiente virtual |
| Argumentos | `-m app.mcp_stdio` |
| Diretório de trabalho | Diretório acessível ao cliente (o pacote deve estar instalado) |
| `OCR_API_URL` | Origem da API, padrão `http://127.0.0.1:8000` |
| `OCR_API_TOKEN` | Credencial individual provisionada, injetada pelo ambiente do processo |

Não grave tokens no repositório, argumentos de ferramentas, linha de comando,
URLs ou capturas. O adaptador lê as duas variáveis do ambiente, sem carregar
o `.env` do servidor. Reinicie o adaptador para trocar sua credencial.
Configuração ausente ou inválida encerra com código 2 e mensagem sem segredo.

O stdio é um cliente REST autenticado. Não importa `app.main` nem configuração,
banco ou fila do servidor; não cria diretório de dados nem threads OCR.
Vários clientes podem usar a mesma instância HTTP. A listagem local de ferramentas
é estática e não consulta credenciais; toda operação de dados passa pela API,
que revalida a identidade e os escopos a cada requisição.

`OCR_API_URL` aceita HTTP apenas em loopback ou uma origem HTTPS explícita.
Não aceita caminho, query, fragmento ou credencial embutida. Requisições não
seguem redirects, não herdam proxy do ambiente e não fazem retries automáticos.
Timeout total de operação: 120 segundos; resposta JSON: até 2 MiB.
O token só é enviado à origem configurada pelo operador. A possibilidade de
configurar HTTPS não substitui a futura homologação/OAuth remoto da AM-13.

## Servidor e erros

O endpoint MCP HTTP continua `/mcp`, com Bearer em todas as requisições e
allowlist de Host/Origin limitado a loopback. O servidor ASGI mantém um único
runtime owner por diretório, compartilhado por HTTP e MCP. Use uma réplica,
`uvicorn --workers 1`; concorrência OCR usa os workers internos.

Preparação de upload e submissão são mutações aditivas; apenas submissão com
chave é idempotente. As demais nove ferramentas são de leitura. As anotações
informam o cliente, mas autorização e validação são aplicadas pelo servidor,
conforme a [especificação de ferramentas MCP](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

Falhas da operação retornam `isError=true` com código estável (por exemplo,
`upload_not_found`, `insufficient_scope`, `idempotency_conflict`,
`queue_unavailable`, `storage_unavailable`). Erros de parâmetros não repetem
valores rejeitados, e argumentos desconhecidos são recusados. Erros da API
no stdio preservam o código, sem encaminhar mensagens internas. Autenticação
inválida no transporte HTTP retorna HTTP 401; protocolo/negociação continuam
sob responsabilidade do SDK oficial 2.3.0.

Fechar o cliente, cancelar uma chamada MCP ou perder a conexão não cancela um
job persistido. Uma admissão cujo recibo se perdeu pode ter sido concluída;
repita a chave original. Após morte do servidor, jobs `processing` retornam a
`queued` na recuperação. Isso garante uma admissão por chave, não execução
exatamente uma vez do motor OCR. Cancelamento explícito é escopo da AM-10.

## Evidência e limites

`tests/test_mcp_vertical.py` usa processos Uvicorn locais, credenciais sintéticas,
PDFs gerados e o cliente oficial `mcp.Client`: Streamable HTTP e subprocesso stdio,
ambos em `auto` e `legacy`. Verifica upload, opções, replay entre transportes,
conflito, polling, texto da segunda página, relatório, hash/PDF baixado,
negação cruzada, escopos, sanitização, limites de resposta e ausência de estado
local no stdio. Outro ensaio mata o servidor em um ponto controlado de
`processing`, reinicia e recupera o mesmo job; um segundo runtime é recusado.

São testes de integração e extração de texto digital. Não comprovam OCR de
imagens, conformidade PDF/A, hosts ChatGPT/Claude, Linux/container ou produção.
Credenciais stdio pelo ambiente seguem a
[orientação de transporte MCP](https://modelcontextprotocol.io/specification/2026-07-28/basic/index).
