# Adaptador OCR para o RAG independente — preparação

`app/services/rag_delivery.py` prepara e envia um pacote `ocr-rag/1`.
Ainda não está ligado às rotas, MCP, startup ou workers do OCR. A produção
permanece na versão anterior; não existe entrega automática habilitada.

## Implementado

- Verificação dos escopos `jobs:read`/`artifacts:read` e propriedade do job.
- Destino configurado pelo operador, vinculado ao proprietário, tipo de fonte
  e processo. A origem não atribui permissões no RAG; o destino valida sua ACL.
- Leitura dentro do lease de artefatos existente. Confere hash e tamanho do
  PDF contra o relatório e a integridade de cada página persistida.
- Pacote imutável em bytes, IDs determinísticos para reenvio, PDF original
  do resultado e JSONL com números de página preservados. Sem metadados
  neste primeiro incremento; não converte classificação processual em norma.
- Uma tentativa HTTP por chamada, HTTPS ou loopback HTTP, sem proxy de
  ambiente ou redirecionamento. Token fora do payload e do repr do destino.
- Recibo limitado a 64 KiB, sem compressão, validado contra manifesto,
  identidade, destino e descritores dos artefatos. `indexed=False` para
  recibos pending/failed. Timeout é resultado incerto, não falha definitiva.
- Nenhum ACK, mesmo indexed, autoriza expurgo ou altera retenção.

Limites compatíveis com o piloto RAG: pacote de artefatos de até 8 MiB,
pages de até 2 MiB, texto de até 200 mil caracteres por página. Páginas
vazias são preservadas, sem texto inventado e sem renumeração das citações.
O receptor deve aceitar páginas vazias antes da ativação deste adaptador.
O destino é configuração interna confiável, não parâmetro de cliente.

## Antes de ativar

O incremento local descrito em [rag-outbox.md](rag-outbox.md) reúne a fila e
o hold no banco do JobStore. Não ativar apenas o sender: a lista abaixo
permanece critério de produção, com limites e pendências detalhados nesse
documento.

1. Admitir entrega somente após autorização, quota e prazo máximo de hold.
   Persistir destino/manifesto/identidade antes de permitir a limpeza.
2. Fazer a limpeza automática, a exclusão explícita e o expurgo físico
   consultarem o hold sob o mesmo lock usado na admissão. Não depender
   somente do lease em memória, que termina ao reiniciar o processo.
3. Persistir estados queued/sending/uncertain/stored/indexed/failed e recibo.
   Uma resposta perdida exige reenvio idempotente do mesmo pacote; não gerar
   outro delivery_id. Coordenar concorrência com lease durável do worker.
4. Definir backoff e teto de tentativas, quota global e por proprietário,
   validade do token e recuperação após reinício. Quota esgotada deve
   rejeitar novas admissões, sem apagar pendências silenciosamente.
5. Definir tratamento do prazo final do hold: alerta/estado de falha,
   política explícita de retenção e cancelamento auditado. O timeout de
   rede não pode liberar o hold. Não manter acervo ilimitado no OCR.
6. Só depois expor API/MCP de solicitação e estado, testar concorrência
   entre limpeza/envio/exclusão e promover uma versão governada.

## Testes

`tests/test_rag_delivery.py` usa PDFs sintéticos processados pelo OCR,
verifica propriedade/escopos, corrupção de resultados, limites do recibo,
redirecionamentos, ausência de retry automático, falhas de rede e ACK.

O teste opcional `test_real_receiver_handoff` requer instalação prévia do
wheel do repositório independente `document-rag`. Ele inicia esse servidor
sem PYTHONPATH do OCR, entrega o resultado real do job, repete sem duplicação,
consulta a página 2, recupera o mesmo PDF e verifica revogação. Não usar
acervo ou configuração de produção para executar esse teste.

Esta preparação ainda requer revisão independente antes do aceite.
