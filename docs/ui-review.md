# Revisão de interface — simplificação e refinamento visual

Base: d49564e (HTTP v1 1.6.0, 30 operações; MCP 0.5.0, 20 tools). Escopo: templates,
CSS e JavaScript do cliente web, mais a página `GET /admin/integrations`. Nenhum
contrato HTTP/MCP, regra de autenticação, autorização, ownership, idempotência ou
retenção foi alterado. Sem dependências, fontes remotas ou framework novos.

## Estrutura

- **Shell comum** (`base.html`, `shell.js`): link para pular ao conteúdo,
  navegação Documentos · Usuários · Integrações (administradores) · Minha conta ·
  Sair, diálogo de confirmação nativo (`<dialog>`), mensagens da API em português.
  Scripts inline saíram dos templates.
- **Documentos**: um `h1`, resumo de `GET /metrics`, horário da última
  sincronização. Painel "Enviar PDFs" (ação primária: escolher PDFs, pasta, arrastar
  ou enviar um único PDF agora), opções de processamento recolhidas com resumo,
  lista de preparação separada do histórico. Histórico com filtros por estado,
  busca, "Mais filtros" (ordenação, período, qualidade, lote), menu Visualização
  (só a aba), barra de seleção contextual (ZIP, cancelar), linhas com uma ação
  principal e "Mais ações". Detalhe em painel lateral com texto paginado,
  relatório legível + JSON integral e zona de exclusão separada.
- **Conta**, **Usuários** e **Integrações** usam os mesmos componentes.
- **Visual**: fontes do sistema, paleta neutra com um acento (`#1f5170`, ~8,5:1
  sobre branco), foco com contorno de 2 px, raios de 6/10 px, bordas para
  estrutura e sombra só em sobreposições, alvos de 44 px em telas de toque,
  `prefers-reduced-motion` respeitado.

## Matriz F01–F29 após a mudança

| ID | Caminho na UI | Situação |
|---|---|---|
| F01 | Enviar um único PDF agora; ou lista com 1 arquivo | Coberta |
| F02 | Lista de preparação → Enviar N PDFs; filtro e resumo de lote | Coberta |
| F03 | Escolher pasta; envia até o limite e mantém o restante na lista | Coberta, sem sequenciamento automático |
| F04 | Estado, busca, período, qualidade, lote, ordenação, "Mostrar mais" | Coberta (filtros locais sobre o histórico completo) |
| F05 | Linha e detalhe atualizados por polling dos ativos | Coberta |
| F06 | Baixar PDF (linha, detalhe) | Coberta |
| F07 | Barra de seleção → Baixar ZIP; manifesto e validade informados | Coberta |
| F08 | Detalhe → Texto por página (seletor, anterior, próximo trecho, copiar) | Coberta |
| F09 | Detalhe → Relatório (resumo + JSON integral) | Coberta |
| F10 | Opções de processamento; reprocessar com mesmas ou atuais | Coberta |
| F11 | Cancelar (linha, detalhe) com confirmação | Coberta |
| F12 | Barra de seleção → Cancelar selecionados | Coberta |
| F13 | Estado "Cancelamento solicitado" | Coberta |
| F14 | Menu da linha / detalhe → Reprocessar | Coberta |
| F15 | Menu da linha / zona de exclusão → Excluir arquivos | Coberta |
| F16 | Resumo de GET metrics; filtro "Em andamento" | Coberta |
| F17 | Opções → Motores e avisos do servidor; limites no painel de envio | Coberta; /health e /readiness são operacionais |
| F18 | Nome no topo; Minha conta | Coberta (GET /me não é necessário na UI) |
| F19 | Minha conta (contrato legado) | Coberta no legado; v1/tool em AM-14 |
| F20 | Login, Sair, troca de senha | Coberta; OAuth/revogação em AM-13 |
| F21 | Usuários (lista e busca) | Coberta no legado; v1 em AM-14 |
| F22 | Usuários → Editar (confirmação ao redefinir senha) | Coberta no legado; v1 em AM-14 |
| F23 | Usuários → Novo usuário (confirmação de senha) | Coberta no legado; política em AM-14 |
| F24 | Integrações (6 operações de service-principals) | Coberta na UI; catálogo MCP em AM-14 |
| F25 | Lista, seleção e detalhe | Coberta |
| F26 | Menu Visualização (ocultar, mostrar, limpar) | Coberta |
| F27 | "Disponíveis até", excluídos/expirados, remoção pendente | Coberta |
| F28 | — | Exceção explícita (infraestrutura) |
| F29 | — | Exceção explícita (extensões futuras) |

## Achados resolvidos

| Achado | Correção |
|---|---|
| Re-render total destruía o foco a cada 5 s e ao marcar itens | Renderização por chave; foco restaurado ao mover/recriar; lista de preparação e detalhe reconstruídos só quando mudam |
| Falha do histórico bloqueava capacidades, envio e polling | Histórico e capacidades carregados de forma independente; polling sempre iniciado; botão para nova consulta de capacidades |
| Pedido de ZIP preso após erro definitivo | 4xx definitivos (incl. 429 antes da gravação) liberam o pedido; rede/408/5xx mantêm chave; "Descartar ZIP pendente" |
| Pedido de retry preso após erro definitivo | Mesma classificação |
| 429 de cota na admissão travava a lista e a limpeza | 429 libera o pedido (nada admitido); uploads validados são reutilizados |
| Modo rápido descartava o arquivo com envio pendente | O arquivo entra na lista e o pedido incerto não é tocado |
| PDF inválido derrubava o lote sem dizer qual | Rejeição por arquivo marcada na lista; os válidos seguem |
| Pasta com mais arquivos que o limite falhava sem saída | Envio limitado a `min(lote, cota − ativos)`, restante permanece |
| Indicadores enganosos (média de progresso, volume, ordenação alfabética de estado) | Removidos; resumo real de `/metrics`; ordenação por prioridade de estado |
| Erros com o mesmo estilo de informação, sem leitura por tecnologia assistiva, em inglês | Aviso persistente com tom, `role="alert"`, tradução por código |
| Foco visível com contraste insuficiente | Contorno sólido de 2 px no acento |
| Exclusão não avisava que impede reprocessar | Texto de confirmação explícito |
| Estados, resultados e retenção em códigos crus | Rótulos em português; o código fica entre parênteses quando é erro |
| Texto só avançava; relatório só em JSON | Seletor de página, anterior, copiar; resumo legível + JSON |
| Sem `h1`, envio dentro de `<nav>`, filtros sem `aria-pressed`, rótulos repetidos, detalhe sem foco/Esc | Corrigidos |
| Arquivos locais misturados ao histórico com ações inválidas | Lista de preparação separada, apenas "Remover" |
| Elementos e CSS mortos (`toast-stack`, `selection-state`, seletores legados) | Removidos |
| Lotes, filtros de período/qualidade/lote, capacidades, métricas, opções por job, manifesto do ZIP e retry herdando opções sem caminho na UI | Expostos com os contratos existentes |
| Credenciais de integração sem tela | `/admin/integrations` |
| Senha de criação sem `autocomplete="new-password"` | Corrigido, com confirmação de senha |
| `service-identities.md` com quatro escopos | Atualizado para cinco |

## Pendências reais

- **AM-14**: perfil e administração em v1/MCP (F19, F21–F23), desativar contas,
  mudar papel, política de senha e o catálogo MCP de metadados/revogação de F24. A UI
  não mostra botões para essas operações.
- **AM-13**: revogação de sessão/JWT e OAuth público. Trocar ou redefinir senha não
  encerra sessões abertas; os textos da UI dizem isso.
- **AM-16**: homologação nos seis hosts MCP.
- **Recomendações a avaliar (não implementadas nesta mudança)**: Content-Security-Policy
  e cabeçalhos de segurança para respostas HTML (agora viável, pois não há scripts
  inline; estilos de largura de progresso ainda usam `style` via JavaScript);
  exigir a senha atual quando o administrador edita a própria conta por
  `/api/users/{id}`; política mínima de senha.
- **Sem sequenciamento automático** de pastas acima do limite: cada envio
  precisa de um clique, para manter uma chave de idempotência por pedido.
- **Arrastar pastas** não percorre subpastas; use "Escolher pasta".
- Ordenação por tamanho preservada para tamanhos conhecidos nesta aba; tamanhos
  desconhecidos ficam ao final, sem consultas de relatório para toda a lista.
- **Validação visual**: contraste, densidade e comportamento em navegadores além do
  Chromium, leitores de tela reais e dispositivos móveis físicos dependem de
  inspeção humana das capturas e de testes manuais.

## Revisão independente do Codex

Após a implementação pelo Claude Code, a inspeção de navegador confirmou e corrigiu
duas regressões: resposta de admissão truncada descartava a chave de idempotência,
e falha isolada nas métricas fazia o resumo afirmar que o histórico estava vazio.
Agora respostas ilegíveis preservam o pedido, e o resumo identifica métricas
indisponíveis com a contagem do histórico carregado. As métricas também acompanham
a atualização de jobs ativos. O painel de detalhes no celular mantém o foco dentro
da área visível, impede interação com o conteúdo encoberto e devolve o foco ao
fechar. Foi removido um texto `null` indevido no estado de jobs concluídos.
Testes em `tests/test_ui_review_followup.py` cobrem esses comportamentos.

## Validação local em 16/10/2026 (UTC)

- Claude Code 2.1.300 executou a auditoria e a implementação em duas fases da
  sessão `d8e8f8d0-e876-4da8-b701-cf5c4b8dceb7`; Codex revisou e validou o resultado.
- Rodada focada: 56 testes aprovados.
- Regressão ampla: 560 aprovados, 1 falha esperada preexistente e 1 expectativa
  de texto desatualizada (`1 selecionado(s)` → `1 selecionado`). A expectativa
  foi corrigida, sem retirar as verificações de foco/seleção, e o teste passou
  na reexecução. Resultado consolidado: 561 casos aprovados e 1 falha esperada.
- Capturas de login, documentos, conta, usuários e integrações em 1440×1000 e
  390×844: sem rolagem horizontal. Capturas adicionais cobrem relatório,
  documentos concluídos/excluídos e administração de credenciais sintéticas.
- Contraste calculado a partir dos tokens CSS finais: texto secundário 5,37:1,
  acento sobre branco 8,50:1 e borda de campos sobre branco 3,52:1. Isso não
  representa certificação completa de acessibilidade.
- Os testes usaram usuários e PDFs sintéticos, com runtime e armazenamento
  isolados. Nenhuma alteração em produção ou no processo licitatório.
