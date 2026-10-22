# Entregas OCR para RAG: API e MCP

Contrato candidato HTTP 1.8.0: 66 operações. MCP 0.7.0: 52 ferramentas.
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
cancelamento também `artifacts:delete`. Todas verificam propriedade, inclusive
recibos e download. Metadados continuam com edição/aprovação somente por MCP.

## Ativação pelo operador

`RAG_CONFIG_FILE` aponta para arquivo JSON privado com caminho absoluto,
limitado a 64 KiB. Nunca versionar tokens. O arquivo contém:

- `policy`: todos os sete campos de `DeliveryPolicy` descritos em rag-outbox.md;
- `destinations`: até 100 entradas com key, url, destination_id, owner,
  source_kind, process_reference e token;
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
