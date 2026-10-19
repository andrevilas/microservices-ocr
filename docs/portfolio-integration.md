# Integração do OCR ao portfólio

Atividade LPM `J7DhQOqJ58XE4xR2hcWp`. Plano documental de 19/10/2026, revisão proposta 1. Preparar todo o OCR para integrações, começando pelo OmniRoute, com evolução governada. Este documento não altera runtime nem comprova integração ativa.

## Fontes e limites da evidência

- [Planilha original](https://docs.google.com/spreadsheets/d/1i2KDEK0ZfWrVsq_iqWapRZzZCwf1dxGeJXu9AfkuWF0/edit), leitura informada de 19/10/2026: aba `Portfólio IA`, A1:I50, conteúdo histórico de 14/10/2026.
- Aba `Serviços`, A1:L70: amostra parcial com Combinado, Medusa e MWG e referências HTTP/MCP; não constitui inventário completo nem contrato autenticado de cada serviço.
- Não foi fornecida revisão imutável ou hash da planilha. Na implementação, registrar export datado, intervalo, hash e responsável pela leitura, sem substituir a proveniência histórica por disponibilidade atual.
- Contratos implementados: [HTTP](http-contract.md), [MCP](mcp-workflow.md), [identidades](service-identities.md), [capacidades](capabilities.md), [resultados](results.md) e [retenção](retention.md). Confirmar a superfície disponível nos contratos e nas capacidades atuais, sem reproduzir contagens de operações ou ferramentas não verificadas.
- Evidência local do OmniRoute e sua separação da VM114: [runbook específico](omniroute-integration.md). Não houve consulta adicional às fontes nesta proposta.

## Arquitetura e autoridade

Nextcloud é o acervo definitivo e a autoridade de ACL. OCR mantém processamento e artefatos temporários; seu proprietário técnico não substitui as permissões do acervo. Elasticsearch atende busca lexical; RAG dedicado atende recuperação vetorial. Talk, Telegram e WhatsApp são canais de bots, sujeitos à mesma autorização documental.

```mermaid
flowchart LR
  C[Consumidor autorizado] -->|HTTP/MCP existente| O[OCR temporário]
  O -. enriquecimento proposto .-> A[Adaptador configurável]
  A -. contrato verificado .-> M[OmniRoute]
  O -. proposta com evidências .-> H[Aprovação humana]
  H -. snapshot aprovado .-> N[Nextcloud e ACL]
  N -. ingestão autorizada .-> S[Elasticsearch + RAG dedicado]
  S -. recuperação filtrada .-> B[Bots Talk / Telegram / WhatsApp]
```

Linhas pontilhadas são fluxos planejados, sem comprovação de implantação. A entrada consome a API/MCP existente; a saída de enriquecimento depende de adaptador, contrato remoto verificado e admissão de orçamento/privacidade. Bloquear ciclos OCR → OmniRoute → OCR por origem, correlação e limite de saltos; o adaptador de saída não recebe capacidade de submeter novamente ao OCR.

## Matriz do portfólio

Categorias: **D** consumidor direto de documentos; **E** rota de enriquecimento; **V** apoio ao desenvolvimento; **O** opcional, sem integração direta justificada. A categoria descreve o uso proposto, não uma API existente. P0 inicia descoberta; P1 depende do núcleo governado; P2 exige caso de uso; P3 não abre integração agora.

| Ferramenta | Categoria / prioridade | Interface proposta | Estado da fonte / lacuna para avançar |
|---|---|---|---|
| OmniRoute | E / P0 | Adaptador de saída após descoberta; consumo de OCR separado | Raiz HTTPS da VM114 em 19/10 redirecionou para login com HTTP 200, sem comprovar API autenticada; launcher local distinto comprovou somente reunião literal; provedores existentes da VM114 permanecem inalterados |
| Open Notebook | D / P1 | Entrega autorizada de texto/PDF e manifesto | Ollama qwen3:14b e bge-m3 citados; confirmar importação, ACL, recibo e retenção; não presumir endpoint |
| Book to Skill | D / P1 | Pacote documental aprovado para processamento explícito | Ferramenta listada; verificar formatos e execução, autoria e destino das skills |
| OpenResearch | D / P2 | Documentos aprovados com citações | Listado; confirmar necessidade, importação, identidade e segregação |
| MiroFish | E / P2 | Enriquecimento de snapshot autorizado | Zep/Ollama citados; confirmar protocolo, persistência, exclusão e custo |
| No AI Slop | V / P2 | Revisão editorial de documentação selecionada | Listado; sem justificativa para acesso automático ao acervo |
| Strix | V / P2 | Avaliação de segurança em ambiente de teste | Listado; escopo e autorização devem ser definidos; sem acesso documental por padrão |
| OpenSEO | O / P3 | Nenhuma agora | Erro DataForSEO e pausa históricos; reavaliar caso de uso antes de contrato |
| I Have ADHD | O / P3 | Nenhuma agora | Listado; benefício documental e interface não demonstrados |
| AI Job Search | O / P3 | Nenhuma agora | Listado; exige caso específico e decisão sobre dados pessoais |
| Open Generative AI | O / P3 | Nenhuma agora | Créditos MuAPI citados; não equivalem a orçamento aprovado nem necessidade OCR |
| Atlas | O / P3 | Nenhuma agora | Listado; finalidade documental e contrato não estabelecidos |
| Pi | O / P3 | Nenhuma agora | Listado; finalidade documental e contrato não estabelecidos |

Esta matriz cobre as 13 ferramentas listadas e a amostra parcial de serviços; não constitui auditoria de todo o inventário. Combinado, Medusa e MWG entram numa descoberta posterior de serviços: registrar consumidor, proprietário, protocolo efetivo e contrato antes de atribuir integração. n8n e outros orquestradores são candidatos; esta leitura não prova instalação. Cada ferramenta precisa demonstrar benefício próprio; não há compromisso de adoção uniforme. Respostas 401 históricas indicam exigência de autenticação e não bastam para classificar um serviço como falho.

## Contratos disponíveis e evolução pendente

Hoje: HTTP v1 e MCP admitem uploads/jobs, consultas, resultados e controle com escopos e posse. Reservar upload não é idempotente; submissão exige chave. Bytes entram por HTTP, não por caminho local ou base64 MCP. Polling respeita `poll_after_seconds`. Texto é paginado por página/offset; relatório oferece proveniência e hashes. Capacidades limitam modos/idiomas; não há downgrade silencioso para extração.

Templates e catálogo de contexto/tipo já possuem operações, versões e hashes. A preparação de vínculos por `MetadataBindingPreparer` e o serviço interno `DocumentMetadataService`, com gravação manual de rascunho, leitura de revisão por ID/hash e listagem de revisões, já foram aceitos. Esses recursos usam o runtime compartilhado e não estão expostos por HTTP ou MCP. A política pura de aprovação prepara um candidato sem persistência; aprovação durável, índice e recibo de aprovação, quota, recuperação e serviço de aprovação continuam pendentes. API, MCP e UI de metadados também permanecem pendentes.

| Etapa | Trabalho proposto / condição de aceite |
|---|---|
| E2 — governança | Completar serviço, persistência, índice, recibo, quotas e recuperação da aprovação sobre a preparação de vínculos e os rascunhos internos já aceitos; concluir API/MCP/UI antes de anunciar acesso externo a metadados |
| E3 — sugestão LLM | Produzir valores propostos com página, hash do texto, offset e citação; validar esquema e evidência; revisão humana antes de promover rascunho |
| E4 — PDF final | Acrescentar XMP e revalidar o PDF/A antes do hash final, guardando proveniência; o runtime já implementa validação fail-closed com veraPDF e presets balanceados, mas a inserção de XMP e sua revalidação final pertencem à evolução E4 |
| E5 — entrega | Obter confirmação do destinatário com hashes exatos e gravar recibo durável antes de iniciar limpeza por TTL do fluxo proposto |
| E6 — recuperação | Indexar somente snapshot aprovado e hash correspondente, com ACL; RAG/GraphRAG filtram permissões antes da recuperação e fornecem citações verificáveis |

O TTL atual do OCR não espera confirmação externa. E5 precisa de coordenação persistente e prazo de entrega inferior à retenção, ou de mudança governada dessa política; não prometer preservação indefinida. GraphRAG é opção posterior, condicionada a benefício e isolamento comprovados.

## Manifesto e recibo propostos

Formato ilustrativo e versionado `portfolio-document/1`, ainda sem endpoint ou schema implantado. O contrato proposto admite múltiplos contextos, tipos e segmentos no mesmo PDF; cada segmento referencia sua versão de esquema, e cada evidência identifica segmento e campo. Hashes abaixo são marcadores a substituir por SHA-256 hexadecimais de 64 caracteres. Não incluir caminhos da máquina de origem, URLs com credenciais ou segredos.

```json
{
  "schema_version": "portfolio-document/1",
  "document_id": "doc-sintetico-01",
  "job_id": "00000000000000000000000000000001",
  "state": "draft",
  "page_count": 2,
  "metadata_schemas": [
    {"context_id": "contexto-a", "type_id": "tipo-a", "type_version": 1, "schema_sha256": "<sha256-schema-a>"},
    {"context_id": "contexto-b", "type_id": "tipo-b", "type_version": 2, "schema_sha256": "<sha256-schema-b>"}
  ],
  "segments": [
    {"segment_id": "segmento-a", "page_start": 1, "page_end": 1, "context_id": "contexto-a", "type_id": "tipo-a", "type_version": 1, "schema_sha256": "<sha256-schema-a>", "metadata": {"numero": "014/2026"}},
    {"segment_id": "segmento-b", "page_start": 2, "page_end": 2, "context_id": "contexto-b", "type_id": "tipo-b", "type_version": 2, "schema_sha256": "<sha256-schema-b>", "metadata": {"valor": "R$ 123,45"}}
  ],
  "evidence": [
    {"segment_id": "segmento-a", "field_name": "numero", "page": 1, "text_sha256": "<sha256-texto-pagina-1>", "offset": 0, "quote": "014/2026"},
    {"segment_id": "segmento-b", "field_name": "valor", "page": 2, "text_sha256": "<sha256-texto-pagina-2>", "offset": 0, "quote": "R$ 123,45"}
  ],
  "artifacts": [{"kind": "pdf", "sha256": "<sha256-pdf-final>", "size_bytes": 1024}],
  "trace": {"event_id": "evt-sintetico-01", "correlation_id": "corr-sintetico-01", "event": "metadata_proposed"}
}
```

Offsets seguem pontos de código Unicode do texto da página; a citação deve coincidir com o trecho no hash indicado. Aprovação gera snapshot imutável separado; edição posterior volta a rascunho e exige nova aprovação.

```json
{
  "schema_version": "portfolio-receipt/1",
  "receipt_id": "recibo-sintetico-01",
  "recipient_id": "destinatario-id",
  "document_id": "doc-sintetico-01",
  "state": "approved",
  "approved_snapshot_sha256": "<sha256-snapshot-aprovado>",
  "manifest_sha256": "<sha256-manifesto>",
  "artifact_sha256": ["<sha256-pdf-final>"],
  "acknowledged_at": "2026-10-19T12:00:00Z",
  "trace_event_id": "evt-sintetico-02"
}
```

Definir serialização canônica e bytes cobertos por cada hash antes de implementar; o hash do manifesto fica no recibo para evitar autorreferência. O estado `approved` exige referência à aprovação humana durável. Um ACK de transporte ou um 200 sem verificação do destinatário não satisfaz E5.

## Segurança, operação e orçamento

Uma identidade de serviço por integração, com concessões mínimas: leitura de resultados usa `jobs:read` + `artifacts:read`; `jobs:submit`, `jobs:cancel` e `artifacts:delete` somente quando necessários. Catálogo exige seus escopos próprios, conforme o contrato. Administrar identidades não concede leitura documental; rotação mantém o mesmo principal.

Serviços não aprovam metadados. A política atual exige principal `user`, o mesmo proprietário humano exato do documento (`user_id` e `subject=user:<owner>`), autoria do HEAD desse proprietário, autenticação `local_jwt` ou `oauth` e todos os escopos exigidos, inclusive para administradores. O registro significa `authorized_principal_review`: JWT/OAuth não comprovam presença humana ou MFA. A aprovação durável ainda precisa reautorizar e conferir HEAD/CAS, concessões, TTL e quota no commit. Delegação humana para documentos pertencentes a serviço está sem solução contratual; não usar impersonação, transferência implícita ou credencial de administrador como atalho. Essa lacuna bloqueia o fluxo correspondente.

Texto, nomes e respostas de modelos são dados não confiáveis. Validar esquema fechado, evidências e tamanho; não executar instruções recuperadas. Controlar concorrência finita, fila limitada, backpressure e quotas por integração. Aplicar timeout e circuit breaker. A política proposta de até três tentativas totais, com backoff limitado, vale somente para leituras HTTP cuja repetição seja segura. Mutações ficam fora dessa política: uma reconciliação de operação comprovadamente idempotente deve preservar chave e corpo conforme seu contrato. Reserva de upload incerta exige reconciliação, não repetição cega. Nenhum comando do launcher admite retry automático; recuperar execução já concluída usa o mesmo ID, sem criar outro para contornar falha ou incerteza. Inferência paga também não recebe retry automático.

Polling é o mecanismo atual. Outbox persistente e webhook assinado são proposta opcional da fase 2, com deduplicação, proteção contra replay e rotação de chave; não anunciar eventos existentes. Esgotados os retries, gravar falha sanitizada em fila de falhas e permitir replay manual autorizado. Respostas 410 exigem encerrar a leitura e reconciliar recibos, nunca contornar retenção.

Orçamento atual: **USD 180, inalterado**. Orçamento de desenvolvimento não reserva nem aprova consumo de IA de serviços. Antes de enriquecimento, definir teto por requisição/período, contabilização, modelo e política de dados. `paid_routes_enabled=false` foi comprovado somente no launcher local; os provedores existentes da VM114 permanecem inalterados, sem evidência de desabilitação global. Sem deploy, envio de documentos à nuvem, segredos compartilhados ou fallback automático para provedor pago.

## Rollout em três fases

1. **Preparar e provar contratos:** concluir C5 com evidência de aceite no plano governado; fechar lacunas E2 e delegação necessárias ao piloto; verificar contrato OmniRoute, manifesto/recibo, ACL, orçamento e cobertura API/MCP. Executar testes sintéticos de contrato, autenticação, escopos, isolamento, custo, retenção/410, timeout, quota e recuperação. Sem esses requisitos, limitar-se à descoberta local.
2. **Canário controlado:** usar documentos sintéticos, uma integração e concorrência mínima; comparar hashes, sugestões, aprovação, ACK e exclusão. Só ampliar a dados autorizados após aceite explícito. Se necessário, implementar e testar outbox/webhook nesta fase. Registrar critério de parada por fuga de ACL, hash divergente, recursão ou estouro de teto.
3. **Ampliar por benefício comprovado:** habilitar consumidores individualmente, com contrato e responsável, métricas de utilidade, custo, latência, erros e recibos. Liberar busca/RAG somente com filtragem e citações testadas. Reverter desabilitando o adaptador e novos despachos, preservando recibos, fila e auditoria para reconciliação; revogar credenciais comprometidas e não apagar evidência para simular rollback.
