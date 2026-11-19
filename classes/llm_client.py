import os
import time
import base64
import io
import wave
import httpx
from google import genai


class LLmClient:
    def __init__(
        self,
        model: str = "gemini-3.5-flash-lite",
        fallback_model: str = "gemini-3.1-flash-lite",
        thinking_level: str | None = None,
        system_instruction: str | None = (
            "Você é um assistente especialista em Controladoria, Custos, Tributos (ICMS, IPI, PIS, COFINS, ISS, IBS, CBS) "
            "e análise de Faturamento, Compras, Financeiro, Estoque e Produção da Nexus (base Datasul). "
            "REGRA FINANCEIRA OBRIGATÓRIA: Tudo o que for PREVISÃO (espécies 'PR', 'PF', 'PV' ou is_previsao = TRUE) NUNCA deve ser contabilizado em Contas a Pagar nem a Receber. Use sempre as views vw_titulos_abertos_pagar e vw_titulos_abertos_receber ou filtre is_previsao = FALSE. "
            "REGRA DE DATA OBRIGATÓRIA (EMISSÃO): Para todos os relatórios e análises de faturamento e compras/entradas de materiais/imobilizado, filtre e apure SEMPRE pela data de emissão (emissao), NUNCA por data de recebimento (dt_docto). "
            "REGRA DE EXCLUSÃO DE OPERAÇÃO ESPECÍFICA (STK CABINES - JANEIRO/2026): A nota fiscal 2149 de Jan/2026 do fornecedor STK CABINES (cliente_fornec = 3335, nat_oper = '1551OO', ser = '1', valor contábil R$ 583.580,00) NUNCA deve ser somada ou considerada em compras, ativo imobilizado ou relatórios. Filtre sempre desconsiderar_metrica = FALSE e NOT (cliente_fornec = 3335 AND doc_fisc = 2149 AND ser = '1' AND nat_oper = '1551OO') ou use as views oficiais. "
            "Utilize as ferramentas MCP para consultar o banco PostgreSQL e responder com precisão."
        ),
    ):
        self.model = model
        self.fallback_model = fallback_model
        self.thinking_level = thinking_level
        self.system_instruction = system_instruction
        self.history: list[dict] = []
        self.previous_interaction_id: str | None = None
        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        http_options = {"httpx_client": httpx.Client(timeout=httpx.Timeout(90.0, connect=10.0))}
        self.client = (
            genai.Client(api_key=api_key, http_options=http_options)
            if api_key
            else genai.Client(http_options=http_options)
        )
        self.client.interactions.sdk_configuration.retry_config = None

    def add_user_message(self, message: str):
        self.history.append({"content": message, "role": "user"})

    def add_assistant_message(self, message: dict):
        self.history.append(message)

    def add_tool_message(self, message: dict):
        self.history.append(message)

    def complete_chat(self, input_data, tools: list[dict] | None = None):
        self.client.interactions.sdk_configuration.retry_config = None

        kwargs = {
            "model": self.model,
            "input": input_data,
        }
        if getattr(self, "thinking_level", None):
            kwargs["generation_config"] = {"thinking_level": self.thinking_level}
        if tools:
            kwargs["tools"] = tools
        if self.system_instruction:
            kwargs["system_instruction"] = self.system_instruction
        if self.previous_interaction_id:
            kwargs["previous_interaction_id"] = self.previous_interaction_id

        for attempt in range(6):
            try:
                interaction = self.client.interactions.create(**kwargs)
                self.previous_interaction_id = interaction.id
                return interaction
            except Exception as e:
                err_text = str(e).lower()
                is_transient = any(
                    code in err_text
                    for code in (
                        "429",
                        "500",
                        "502",
                        "503",
                        "504",
                        "deadline",
                        "unavailable",
                        "high demand",
                        "spikes in demand",
                        "timed out",
                        "timeout",
                        "connection",
                    )
                )
                if attempt < 5 and is_transient:
                    # Alterna entre o modelo atual e o fallback
                    if kwargs["model"] == self.model and self.fallback_model:
                        kwargs["model"] = self.fallback_model
                    else:
                        kwargs["model"] = self.model
                    
                    # Espera progressiva: 1.5s, 3.0s, 4.5s...
                    wait_sec = 1.5 * (attempt + 1)
                    time.sleep(wait_sec)
                    continue
                raise
        raise RuntimeError("Falha ao comunicar com a API do Gemini após 6 tentativas.")

    def synthesize_speech(self, text: str) -> bytes:
        """Gera uma resposta em português brasileiro no formato WAV."""
        response = self.client.models.generate_content(
            model="gemini-3.1-flash-tts-preview",
            contents=(
                "Leia o texto abaixo em português brasileiro, com voz clara e tom profissional. "
                "Não acrescente informações.\n\n" + text
            ),
            config={
                "response_modalities": ["AUDIO"],
                "speech_config": {
                    "language_code": "pt-BR",
                    "voice_config": {
                        "prebuilt_voice_config": {"voice_name": "Charon"}
                    },
                },
            },
        )

        audio_part = next(
            (
                part.inline_data
                for candidate in (response.candidates or [])
                for part in (candidate.content.parts if candidate.content else [])
                if part.inline_data and part.inline_data.data
            ),
            None,
        )
        if audio_part is None:
            raise RuntimeError("O serviço de voz não retornou áudio.")

        pcm = audio_part.data
        if isinstance(pcm, str):
            pcm = base64.b64decode(pcm)

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm)
        return wav_buffer.getvalue()

