# Entregas OCR para RAG: API e MCP

Contrato candidato HTTP 1.9.0: 69 operações. MCP 0.7.0: 55 ferramentas.
Esta integração está disponível no código candidato; não constitui registro
de implantação em produção. A configuração permanece desabilitada por padrão.

| HTTP sob `/api/v1/rag` | Ferramenta MCP | Finalidade |
|---|---|---|
| GET `/destinations` | `ocr_list_rag_destinations` | Destinos vinculados ao proprietário |
| POST `/deliveries` | `ocr_submit_rag_delivery` | Admitir pacote na fila persistente |
| GET `/deliveries/{id}` | `ocr_get_rag_delivery` | Consultar estado |
| GET `/deliveries/{id}/receipt` | `ocr_get_rag_receipt` | Último recibo validado |
| GET `/deliveries/{id}/recovery` | `ocr_get_rag_recovery` | URL relativa autenticada, hash e tamanho |
| GET `/deliveries/{id}/recovery/content` | Download HTTP | Bytes do pacote retido |
| POST `/deliveries/{id}/cancel` | `ocr_cancel_rag_delivery` | Cancelar e liberar hold explicitamente |
| GET `/deliveries?state=&limit=` | `ocr_list_rag_deliveries` | Listar entregas (administrador vê todas) |
| POST `/deliveries/{id}/retry` | `ocr_retry_rag_delivery` | Devolver entrega em resolução à fila |
| GET `/operations` | `ocr_get_rag_operations` | Estado agregado do worker (administrador) |

O MCP remoto e o adaptador stdio usam as mesmas operações. Nenhuma ferramenta
transfere o pacote inteiro. Cada requisição autentica o principal atual.
Os clientes recebem somente aliases de destinos autorizados: URLs, tokens e
classificações não são fornecidos pelo cliente na admissão.

```json
{"job_id":"11111111111111111111111111111111","destination":"normas"}
```

A mesma combinação de job e destino repete a entrega persistida. O ID retornado
tem 64 caracteres hexadecimais. Em caso de timeout, repetir a mesma solicitação
e consultar esse ID. Admissão não significa indexação. Recibo com indexing_state
pending não é confirmação indexed. Um cancelamento não desfaz conteúdo que já
tenha chegado ao receptor; ele permite a limpeza local de arquivos vencidos.

Consultas exigem `jobs:read`; admissão e recuperação também `artifacts:read`;
cancelamento também `artifacts:delete`; nova tentativa também `jobs:submit`.
Todas verificam propriedade, inclusive recibos e download. Um administrador
humano pode listar, consultar, repetir e cancelar entregas de qualquer
proprietário; esses atos ficam auditados como `operator-*`. Recibos e pacotes
de recuperação continuam restritos ao proprietário.

## Resolução

- `resolution` bloqueia novas admissões apenas do mesmo proprietário.
- Capacidade (`max_entries`, `max_payload_bytes` e o opcional
  `max_entries_per_owner`) conta somente entregas ativas; `indexed` e
  `canceled` não ocupam vagas.
- `retry` exige pacote retido, zera tentativas e renova o prazo de hold uma vez
  pela política da própria entrega. Nada é apagado.
- Reenviar job/destino após `canceled` recoloca a mesma entrega na fila com o
  pacote reconstruído; o receptor devolve o mesmo recibo se já a tinha.
- HTTP 401/403/404 do receptor viram `rag_destination_unauthorized`, tratado
  como condição transitória de operador (credencial/vínculo), com backoff.
- `claim_seconds` mínimo de 60 s, para que uma tentativa HTTP limitada não
  seja descartada como `superseded`. Metadados continuam com edição/aprovação somente por MCP.

## Ativação pelo operador

`RAG_CONFIG_FILE` aponta para arquivo JSON privado com caminho absoluto,
limitado a 64 KiB. Nunca versionar tokens. O arquivo contém:

- `policy`: os sete campos obrigatórios de `DeliveryPolicy` (e o opcional `max_entries_per_owner`) descritos em rag-outbox.md;
- `destinations`: até 100 entradas com key, url, destination_id, owner,
  source_kind, process_reference e token; `destination_id`,
  `source_kind` e `process_reference` são validados no startup com as mesmas
  regras do manifesto `ocr-rag/1`. URL: HTTPS, HTTP em loopback ou HTTP para
  nome de serviço Kubernetes `<svc>.<namespace>.svc.cluster.local.` (rede
  interna do nó). Usar a forma com ponto final: o nome é absoluto e não
  passa pelos sufixos de busca do resolvedor (ndots:5), então só o DNS do
  cluster responde. O OCR precisa rodar como pod do mesmo cluster. Hosts de LAN/Internet exigem HTTPS;
- `interval_seconds`, `batch_size`, `warning_seconds`, `shutdown_seconds`:
  valores explícitos, sem prazos de produção assumidos pelo código.

O arquivo é lido na inicialização, após adquirir o lease e fixar o JobStore.
Erros de configuração interrompem startup com mensagem sem credenciais.
Sem arquivo, operações respondem rag_unavailable. Alterações do arquivo requerem
reinício controlado. Rotação não deve mudar o vínculo de uma entrega já admitida.

O worker inicia dentro do lifespan. No encerramento, o serviço deixa de aceitar
chamadas e aguarda o worker. Se ele continuar ativo, o lease não é liberado nem
substituído. Não iniciar outra instância sobre o mesmo diretório.

Antes de produção ainda são exigidos quota global de armazenamento, política
operacional de resolução/alertas, revisão independente e ensaio de release e
rollback. Os limites da fila não equivalem a quota de disco ou garantia WORM.

## Grafo (ocr-rag/2)

`include_graph=true` na admissão (com `metadata:read`) acrescenta os metadados
aprovados e o `ocr-graph/1` ao pacote; `metadata_revision_id` fixa a revisão.
Cada destino pode declarar `graph_sensitivity_ceiling` (`public`, `internal`
ou `restricted`; padrão `internal`) no arquivo de configuração. Detalhes,
identidade e verificação de evidências em [graph-export.md](graph-export.md).
