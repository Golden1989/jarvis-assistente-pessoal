"""Lista os modelos disponíveis para a sua conta, direto da API."""

import anthropic

client = anthropic.Anthropic()

for modelo in client.models.list():
    print(modelo.id, "—", modelo.display_name)

# Detalhes de um modelo específico (janela de contexto, teto de saída):
print()
info = client.models.retrieve("claude-sonnet-5")
print("ID:            ", info.id)
print("Nome:          ", info.display_name)
print("Contexto (in): ", getattr(info, "max_input_tokens", "n/d"))
print("Saída máx:     ", getattr(info, "max_tokens", "n/d"))
