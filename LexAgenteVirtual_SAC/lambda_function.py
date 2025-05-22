import json
import boto3
import requests
import os

def lambda_handler(event, context):
    print("📥 Evento recibido:", json.dumps(event, indent=2))

    try:
        session_state = event.get("sessionState", {})
        intent = session_state.get("intent", {})
        intent_name = intent.get("name", "")
        session_attributes = session_state.get("sessionAttributes", {}) or {}
        input_transcript = event.get("inputTranscript", "").lower()
        slots = intent.get("slots", {})
        
        # Obtener config solo una vez
        config = obtener_secret("main/LexAgenteVirtualSAC")
        # -----------------------------
        # ¿Está esperando una respuesta final tipo "¿puedo ayudarte con algo más?"?
        # -----------------------------
        if session_attributes.get("esperando_respuesta_final") == "true":
            print("🔄 Procesando posible intención post-respuesta...")
            session_attributes.pop("esperando_respuesta_final", None)
            return manejar_respuesta_post_pregunta_adicional(input_transcript, session_attributes)

        # 🔄 Si la intención cambia, limpiamos la bandera de espera de respuesta final
        if session_attributes.get("esperando_respuesta_final") == "true" and intent_name != "SaludoHabeasData":
            session_attributes.pop("esperando_respuesta_final", None)

        print(f"📌 Intent detectado: {intent_name}")
        print(f"📌 Frase del usuario: {input_transcript}")
        print(f"📌 Session Attributes actuales: {session_attributes}")

        # -----------------------------
        # 🔒 Validación centralizada de aceptación de políticas
        # -----------------------------
        if session_attributes.get("acepto_politicas") != "true" and intent_name != "SaludoHabeasData":
            print("🔁 Redirigiendo a SaludoHabeasData porque aún no se aceptan políticas.")

            if not session_attributes.get("politicas_mostradas"):
                session_attributes["politicas_mostradas"] = "true"
                return responder(
                    "Bienvenid@ al Servicio al Cliente de MiEmpresa! "
                    "Al continuar con esta comunicación estás de acuerdo con nuestra política de manejo de datos: "
                    "https://miempresa/tratamiento-de-informacion\n\n¿Deseas continuar?",
                    session_attributes,
                    "SaludoHabeasData"
                )

            # Usuario acepta las políticas
            if any(p in input_transcript for p in ["si", "sí", "acepto", "de acuerdo", "vale", "claro", "ok", "bueno", "listo", "está bien"]):
                session_attributes["acepto_politicas"] = "true"
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "intent": {"name": "SaludoHabeasData", "state": "Fulfilled"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [
                        {
                            "contentType": "PlainText",
                            "content": "¡Gracias por aceptar nuestras políticas! ¿Dime en qué te puedo ayudar?"
                        }
                    ]
                }

            # Usuario rechaza las políticas
            if any(p in input_transcript for p in ["no", "rechazo", "no acepto"]):
                return cerrar_conversacion(
                    "Gracias por contactarte con nosotros. Lamentablemente no podemos continuar si no aceptas nuestras políticas de tratamiento de datos.",
                    "SaludoHabeasData"
                )

            # Aún no responde con claridad
            return responder("¿Deseas continuar y aceptar nuestras políticas de tratamiento de información?", session_attributes, "SaludoHabeasData")

        # -----------------------------
        # FLUJO: SALUDO + HÁBEAS DATA
        # -----------------------------
        if intent_name == "SaludoHabeasData":
            saludos_validos = ["hola", "buenas", "saludos", "hey", "qué tal", "buenos días", "buenas tardes"]

            if session_attributes.get("acepto_politicas") == "true":
                if any(s in input_transcript for s in saludos_validos):
                    return responder("¡Hola nuevamente! ¿En qué más puedo ayudarte?", session_attributes, intent_name)
                else:
                    print("⚠️ Frase clasificada como saludo pero no parece un saludo real. Mostrando sugerencias.")
                    return mostrar_sugerencias(session_attributes)

            if session_attributes.get("politicas_mostradas") == "true":
                if any(p in input_transcript for p in ["si", "sí", "acepto", "de acuerdo", "vale", "claro", "ok", "bueno", "listo", "está bien"]):
                    session_attributes["acepto_politicas"] = "true"
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitIntent"},
                            "intent": {"name": intent_name, "state": "Fulfilled"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": "¡Gracias por aceptar nuestras políticas! ¿Dime en qué te puedo ayudar?"
                        }]
                    }

                if any(p in input_transcript for p in ["no", "rechazo", "no acepto"]):
                    return cerrar_conversacion(
                        "Gracias por contactarte con nosotros. Lamentablemente no podemos continuar si no aceptas nuestras políticas de tratamiento de datos.",
                        intent_name
                    )

                return responder("¿Deseas continuar y aceptar nuestras políticas de tratamiento de información?", session_attributes, intent_name)

            # Primer contacto con esta intención
            session_attributes["politicas_mostradas"] = "true"
            mensaje = (
                "Bienvenid@ al Servicio al Cliente de MiEmpresa! "
                "Al continuar con esta comunicación estás de acuerdo con nuestra política de manejo de datos: "
                "https://miempresa/tratamiento-de-informacion\n\n¿Deseas continuar?"
            )
            return responder(mensaje, session_attributes, intent_name)

        # -----------------------------
        # FLUJO: ConsultaInfoPlan
        # -----------------------------

        if intent_name == "ConsultaInfoPlan":
            try:
                # 1. Validar tipo y número de documento (centralizado)
                document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                    slots, session_attributes, input_transcript, intent
                )

                if respuesta_incompleta:
                    return respuesta_incompleta

                print(f"✅ Tipo documento mapeado: {document_type_id}, Número: {document_number}")
                print("🕐 Enviando mensaje de espera al usuario...")
                respuesta_espera = responder("Un momento por favor, estamos consultando la información de tu plan...", session_attributes, intent_name)
                print("🟡 Esperando mientras se realiza consulta API")

                # 2. Consultar plan
                datos_plan, error_msg = consultar_plan(document_type_id, document_number)

                if error_msg:
                    return responder(error_msg, session_attributes, intent_name)

                # 3. Guardar info del plan en sesión
                session_attributes["datos_plan_json"] = json.dumps(datos_plan)

                # 4. Generar respuesta usando Bedrock (sin KB)
                mensaje_final = respuesta_bedrock(intent_name, datos_plan)
                session_attributes["esperando_respuesta_final"] = "true"

                return responder(mensaje_final, session_attributes, intent_name)

            except Exception as e:
                print("❌ Error en ConsultaInfoPlan:", str(e))
                return responder("Lo siento, ha ocurrido un error al procesar tu solicitud. Intenta nuevamente más tarde.", session_attributes, intent_name)

        # -----------------------------
        # 4️⃣ FLUJO: FQABodytech
        # -----------------------------
        if intent_name == "FQABodytech":
            try:
                
                prompt = get_prompt_por_intent(intent_name, input_transcript)
                respuesta_kb = consultar_kb_bedrock(prompt, config["BEDROCK_KB_ID_FQABodytech"])
                mensaje_final = f"{respuesta_kb.strip()}\n\n¿Puedo ayudarte con algo más? 🤗"
                session_attributes["esperando_respuesta_final"] = "true"

                return responder(mensaje_final, session_attributes, intent_name)
            except Exception as e:
                print("❌ Error en FQABodytech:", str(e))
                return responder("Lo siento, hubo un problema consultando la información. Intenta más tarde.", session_attributes, intent_name)

        # -----------------------------
        # FLUJO: Venta
        # -----------------------------
        if intent_name == "Venta":
            try:
                prompt = get_prompt_por_intent(intent_name, input_transcript)
                kb_id = config.get("BEDROCK_KB_ID_Venta")

                if kb_id:
                    respuesta_kb = consultar_kb_bedrock(prompt, kb_id)
                    mensaje_final = respuesta_kb.strip()
                    session_attributes["esperando_respuesta_final"] = "true"
                else:
                    campaign_id = config.get("campain_ventas", "1")
                    mensaje_final = f"🛍️ ¡Gracias por tu interés!\nUn asesor de nuestro equipo estará contigo en breve para ayudarte con tu compra 😊"
                    session_attributes["esperando_respuesta_final"] = "true"
                return responder(mensaje_final, session_attributes, intent_name)

            except Exception as e:
                print("❌ Error en intención Venta:", str(e))
                return responder("Lo siento, hubo un problema procesando tu solicitud. Intenta más tarde.", session_attributes, intent_name)

        # -----------------------------
        # FLUJO: CongelarPlan
        # -----------------------------

        if intent_name == "CongelarPlan":
            try:
                # 1. Revisar si ya hay info del plan en sesión
                datos_plan_json = session_attributes.get("datos_plan_json")
                if datos_plan_json:
                    print("♻️ Usando información de plan existente en sesión")
                    datos_plan = json.loads(datos_plan_json)
                else:
                    # 2. Validar tipo y número de documento (con función genérica)
                    document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                        slots, session_attributes, input_transcript, intent
                    )

                    if respuesta_incompleta:
                        return respuesta_incompleta

                    print(f"✅ Tipo documento mapeado: {document_type_id}, Número: {document_number}")
                    print("🕐 Enviando mensaje de espera al usuario...")
                    respuesta_espera = responder("Un momento por favor, estamos consultando tu plan para validar si puedes congelarlo...", session_attributes, intent_name)
                    print("🟡 Esperando mientras se realiza consulta API")

                    # 3. Consultar plan
                    datos_plan, error_msg = consultar_plan(document_type_id, document_number)

                    if error_msg:
                        return responder(error_msg, session_attributes, intent_name)

                # 4. Verificar si el plan permite congelación (is_recurring)
                is_recurring = None
                try:
                    planes = datos_plan.get("data", {}).get("plans", [])
                    if planes and isinstance(planes, list):
                        is_recurring = planes[0].get("is_recurring")
                except Exception as e:
                    print("⚠️ Error al extraer is_recurring:", str(e))

                # 5. Generar mensaje final según el tipo de plan
                mensaje = obtener_respuesta_congelacion(is_recurring)
                session_attributes["esperando_respuesta_final"] = "true"

                return responder(mensaje, session_attributes, intent_name)

            except Exception as e:
                print("❌ Error en CongelarPlan:", str(e))
                return responder("Lo siento, hubo un error al validar la congelación de tu plan. Intenta más tarde.", session_attributes, intent_name)

        # -----------------------------
        # FLUJO: FQAReferidos
        # -----------------------------
        if intent_name == "FQAReferidos":
            try:
                prompt = get_prompt_por_intent(intent_name, input_transcript)
                respuesta_kb = consultar_kb_bedrock(prompt, config["BEDROCK_KB_ID_FQAReferidos"])
                mensaje_final = respuesta_kb.strip()
                session_attributes["esperando_respuesta_final"] = "true"

                return responder(mensaje_final, session_attributes, intent_name)
            except Exception as e:
                print("❌ Error en FQAReferidos:", str(e))
                return responder("Lo siento, hubo un problema consultando la información. Intenta más tarde.", session_attributes, intent_name)


        # -----------------------------
        # FLUJO: Fallback personalizado
        # -----------------------------
        if intent_name == "FallbackIntent":
            return mostrar_sugerencias(session_attributes)


    except Exception as e:
        print("❌ Error general en Lambda:", str(e))
        return responder("Lo siento, ha ocurrido un error inesperado.", {}, "FallbackIntent")


# --------------------- #
# FUNCIONES AUXILIARES  #
# --------------------- #

##################
# Obtener Secret #
##################

def obtener_secret(secret_name):
    print(f"🔐 Obteniendo secret: {secret_name}")
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response['SecretString'])

########################
# Consultar KB Bedrock #
########################

def consultar_kb_bedrock(prompt, kb_id):
    print("🤖 Enviando prompt a Bedrock:")
    print(prompt)

    client = boto3.client("bedrock-agent-runtime")
    response = client.retrieve_and_generate(
        input={"text": prompt},
        retrieveAndGenerateConfiguration={
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0"
            },
            "type": "KNOWLEDGE_BASE"
        }
    )
    print("✅ Respuesta recibida desde Bedrock")
    return response["output"]["text"]

#############
# Respuesta #
#############

def responder(mensaje, session_attributes, intent_name):
    print("📤 Enviando respuesta a Lex:", mensaje)
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": intent_name,
                "state": "Fulfilled"
            },
            "sessionAttributes": session_attributes
        },
        "messages": [
            {
                "contentType": "PlainText",
                "content": mensaje
            }
        ]
    }

#######################
# Cerrar Conversacion #
#######################

def cerrar_conversacion(mensaje, intent_name):
    print("🔒 Cerrando conversación:", mensaje)
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": intent_name,
                "state": "Fulfilled"
            },
            "sessionAttributes": {}
        },
        "messages": [
            {
                "contentType": "PlainText",
                "content": mensaje
            }
        ]
    }

###
# Sugerencias de intension
###

def mostrar_sugerencias(session_attributes):
    sugerencias = (
        "Lo siento, no logré identificar tu solicitud 🤔.\n"
        "Pero puedo ayudarte con:\n"
        "📄 Preguntas frecuentes\n"
        "🛍️ Comprar un plan\n"
        "📅 Información sobre tu plan\n\n"
        "¿Sobre cuál tema necesitas ayuda?"
    )
    return responder(sugerencias, session_attributes, "FallbackIntent")

###
# Respuesta Post Pregunta Adicional
###

def manejar_respuesta_post_pregunta_adicional(input_transcript, session_attributes):
    negativas = ["no", "no gracias", "nada más", "estoy bien", "ninguna"]
    afirmativas = ["sí", "si", "claro", "vale", "de acuerdo", "otra", "quiero saber"]

    if any(p in input_transcript.lower() for p in negativas):
        return cerrar_conversacion("Gracias por contactarte con nosotros. ¡Feliz día! 😊", "Despedida")

    elif any(p in input_transcript.lower() for p in afirmativas):
        return mostrar_sugerencias(session_attributes)

    # Si no es claro, se asume que podría ser una intención, entonces se clasifica con Bedrock
    try:
        prompt = f"""
Usuario dijo: \"{input_transcript}\"
Clasifica este mensaje en una de estas intenciones:
- FQABodytech
- Venta
- ConsultaInfoPlan

Devuelve solo una palabra (el nombre de la intención). Si no aplica ninguna, responde: Desconocido
"""
        config = obtener_secret("main/LexAgenteVirtualSAC")
        intencion_detectada = consultar_kb_bedrock(prompt, config["BEDROCK_KB_ID_FQABodytech"]).strip()

        if intencion_detectada in ["FQABodytech", "Venta", "ConsultaInfoPlan", "CongelarPlan"]:
            print(f"✅ Disparando intención detectada: {intencion_detectada}")
            return {
                "sessionState": {
                    "dialogAction": {"type": "ElicitIntent"},
                    "intent": {
                        "name": intencion_detectada,
                        "state": "InProgress",
                        "slots": {}  # Puedes mantener los slots vacíos o conservar los existentes si aplica
                    },
                    "sessionAttributes": session_attributes
                },
                "messages": [
                    {"contentType": "PlainText", "content": f"¡Perfecto! Vamos a ayudarte"}
                ]
            }


    except Exception as e:
        print("❌ Error al clasificar con Bedrock:", str(e))

    return responder("Lo siento, no logré entenderte. ¿Sobre cuál tema necesitas ayuda? 🤔", session_attributes, "FallbackIntent")


###
# Obtener Token
###

def obtener_token_dinamico(config):
    print("🔐 Solicitando token OAuth dinámico...")

    token_url = config.get("TOKEN_URL")
    client_id = config.get("CLIENT_ID")
    client_secret = config.get("CLIENT_SECRET")

    if not all([token_url, client_id, client_secret]):
        print("❌ Configuración incompleta para obtener token")
        raise Exception("Faltan datos para autenticación")

    token_payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }
    token_headers = {"Content-Type": "application/json"}

    token_response = requests.post(token_url, json=token_payload, headers=token_headers)

    if token_response.status_code != 200:
        print("❌ Error obteniendo token:", token_response.status_code, token_response.text)
        raise Exception("Error obteniendo token")

    token_data = token_response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        raise Exception("Token de acceso no recibido")

    print("✅ Token obtenido correctamente")
    return access_token

###
# Consulta Tipo y Numero de Documeto  
###

def validar_documento_usuario(slots, session_attributes, input_transcript, intent):
    print("🔍 Validando tipo y número de documento...")

    tipo_documento_map = {
        "ke": 2, "carnet de extranjería": 2,
        "sc": 5, "salvoconducto de permanencia": 5,
        "nit": 9, "número de identificación tributaria": 9,
        "cc": 10, "cedula": 10, "cédula ciudadanía": 10,
        "dni": 11, "documento nacional de identidad": 11,
        "ruc": 12, "registro único de contribuyentes": 12,
        "ce": 20, "cedula extranjera": 20, "extranjería": 20,
        "pp": 30, "pasaporte": 30,
        "ti": 50, "tarjeta de identidad": 50
    }

    # ✅ Caso 1: Ya está en sesión
    if "document_type_id" in session_attributes and "document_number_val" in session_attributes:
        print("✅ Tipo y número de documento ya existen en session_attributes")
        return int(session_attributes["document_type_id"]), session_attributes["document_number_val"], session_attributes, None

    # ✅ Caso 2: Buscar en slots
    document_type_raw = slots.get("document_type", {}).get("value", {}).get("interpretedValue", "") if slots else ""
    document_number = slots.get("document_number", {}).get("value", {}).get("interpretedValue", "") if slots else ""

    # ✅ Caso 3: Input directo
    if not document_number and input_transcript and input_transcript.replace(" ", "").isalnum():
        print("ℹ️ Intentando capturar número desde input directo")
        document_number = input_transcript.strip()

    print("📌 Document Type recibido:", document_type_raw)
    print("📌 Document Number recibido:", document_number)

    # Buscar coincidencia parcial entre las posibles interpretaciones
    document_type_id = None
    if document_type_raw:
        for parte in document_type_raw.lower().split(","):
            parte = parte.strip()
            if parte in tipo_documento_map:
                document_type_id = tipo_documento_map[parte]
                break

    # ⚠️ Validación de datos
    if not document_type_id or not document_number or len(document_number) < 5:
        print("⚠️ Datos incompletos o inválidos")
        slot_faltante = "document_number" if document_type_id else "document_type"
        respuesta = {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": slot_faltante},
                "intent": {
                    "name": intent.get("name"),
                    "slots": intent.get("slots", {}),
                    "state": "InProgress",
                    "confirmationState": "None"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [
                {
                    "contentType": "PlainText",
                    "content": "Por favor, proporciona un tipo y número de documento válido."
                }
            ]
        }
        return None, None, session_attributes, respuesta

    # ✅ Guardar en sesión
    session_attributes["document_type_id"] = str(document_type_id)
    session_attributes["document_number_val"] = document_number
    print(f"✅ Tipo documento mapeado: {document_type_id}, Número: {document_number}")

    return document_type_id, document_number, session_attributes, None


###############################
# Consulta Plan               # 
###############################

def consultar_plan(document_type, document_number):
    print("📞 Iniciando consulta de plan...")

    try:
        config = obtener_secret("main/LexAgenteVirtualSAC")
        api_url = config.get("API_URL_INFO_PLAN")
        access_token = obtener_token_dinamico(config)

        if not access_token:
            print("❌ ACCESS_TOKEN no encontrado en el secret.")
            return None, "Lo siento, hubo un problema autenticando tu solicitud. Intenta más tarde."

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }
        payload = {
            "document_type": str(document_type),
            "document_number": document_number
        }

        response = requests.post(api_url, json=payload, headers=headers, timeout=20)

        if response.status_code != 200:
            print(f"❌ Error en la llamada al API: {response.status_code} - {response.text}")
            return None, "No encontramos información del plan asociada a ese documento. Verifica los datos o intenta más tarde."

        datos = response.json()
        print("✅ Consulta de plan exitosa:", datos)
        return datos, None

    except Exception as e:
        print("❌ Error al consultar el plan:", str(e))
        return None, "Lo siento, hubo un problema consultando el plan. Intenta más tarde."


###############################
# Consulta Plan es Recurrente # 
###############################

def obtener_is_recurring_desde_json(session_attributes):
    try:
        datos_json = session_attributes.get("datos_plan_json") or session_attributes.get("info_plan")
        if not datos_json:
            return None
        
        parsed = json.loads(datos_json)
        plans = parsed.get("data", {}).get("plans", [])
        if not plans:
            return None
        
        return plans[0].get("is_recurring", None)
    except Exception as e:
        print("❌ Error al extraer is_recurring:", str(e))
        return None

##################################
# Resumen Informacion Plan       # 
##################################

def obtener_resumen_plan(datos_plan: dict) -> str:
    """
    Resume los datos del plan en texto plano para usar en el prompt de Bedrock.
    Esto evita enviar el JSON completo y mejora el rendimiento.
    """
    try:
        data = datos_plan.get("data", {})
        nombre = f"{data.get('name', '')} {data.get('last_name', '')}".strip()
        plan = data.get("plans", [])[0] if data.get("plans") else {}

        resumen = (
            f"Nombre: {nombre}\n"
            f"Tipo de plan: {plan.get('label', 'N/A')}\n"
            f"Estado: {plan.get('status', 'N/A')}\n"
            f"Inicio: {plan.get('date_start', 'N/A')}\n"
            f"Vencimiento: {plan.get('date_end', 'N/A')}\n"
            f"Sede: {plan.get('venue_use', 'N/A')}\n"
            f"Recurrente: {'Sí' if plan.get('is_recurring') else 'No'}"
        )
        return resumen
    except Exception as e:
        print("❌ Error generando resumen:", str(e))
        return "Información no disponible"


#####################################
# Consulta Bedrock Lenguaje Natural # 
#####################################

def respuesta_bedrock(intent_name: str, contenido_json: dict, modelo: str = "anthropic.claude-3-sonnet-20240229-v1:0") -> str:
    """
    Genera una respuesta en lenguaje natural usando Bedrock sin KB,
    a partir del resumen del JSON de entrada y un prompt personalizado por intención.
    """
    import boto3
    import json

    client = boto3.client("bedrock-runtime")

    # ✅ 1. Generar resumen y construir prompt
    resumen = obtener_resumen_plan(contenido_json)
    prompt = get_prompt_por_intent(intent_name, resumen)

    print("🧠 Prompt enviado a Bedrock:")
    print(prompt)

    # ✅ 2. Enviar al modelo
    response = client.invoke_model(
        body=json.dumps({
            "prompt": prompt,
            "max_tokens_to_sample": 500,
            "temperature": 0.7
        }),
        modelId=modelo,
        accept="application/json",
        contentType="application/json"
    )

    # ✅ 3. Leer respuesta
    respuesta = json.loads(response["body"].read().decode())
    return respuesta.get("completion", "").strip()


####
# Respuestas Congelacion
###


def obtener_respuesta_congelacion(is_recurring):
    if is_recurring:
        return (
            "👋 ¡Hola! Congelar tu plan es muy fácil. Te cuento: "
            "🧊 ¿Cómo lo haces? "
            "• Ingresa a 👉 https://www.bodytech.com.co "
            "• Ve a tu perfil y entra en la sección 'Novedades'. "
            "📆 ¿Cada cuánto puedo congelar? "
            "• Puedes congelar tu plan las veces que quieras. "
            "• Cada congelación debe ser de mínimo 7 días y máximo 30 días. "
            "💵 ¿Tiene costo? "
            "• Solo pagas una cuota de mantenimiento. "
            "• Es el 25% del valor del tiempo congelado. "
            "• Cubre sede y equipos 🏋️. "
            "⚠️ Importante: la congelación solo aplica hacia el futuro, no se puede hacer retroactiva."
        )
    else:
        return (
            "👋 ¡Hola! Congelar tu plan es muy fácil. Te cuento: "
            "🧊 ¿Cómo lo haces? "
            "• Ingresa a 👉 https://www.bodytech.com.co "
            "• Ve a tu perfil y entra en la sección 'Novedades'. "
            "📆 ¿Cada cuánto puedo congelar? "
            "• Puedes congelar tu plan 2 veces al año. "
            "• Cada congelación debe ser de mínimo 7 días y máximo 30 días. "
            "⚠️ Importante: la congelación solo aplica hacia el futuro, no se puede hacer retroactiva."
        )


    return contenido

# -----------------------------
# PROMPT (mantener igual)
# -----------------------------

def get_prompt_por_intent(intent_name, contenido):
 
##### 
## Informacion de Plan
#####

    if intent_name == "ConsultaInfoPlan":
        return f"""
Eres un asistente virtual de servicio al cliente de Bodytech. Tu objetivo es brindar información del plan de forma clara, cálida y profesional.

🧩 ¿Qué debes hacer?

1. Lee el JSON que contiene los datos del plan.
2. Si no hay plan activo, responde con un mensaje cordial informando eso.
3. Si hay plan, incluye:
   - Saludo personalizado si hay nombre
   - Tipo de plan
   - Fecha de inicio y fin
   - Estado
   - Extras como sede, modalidad, etc. si están presentes

📌 Formato de respuesta:
• Usa viñetas con bullet (•)
• Añade emojis moderados (💪, ✅, 🏋️, 😊)
• Sé directo, empático, evita repetir preguntas

🎯 Finaliza SIEMPRE con:
“¿Puedo ayudarte con algo más? 🤗”

Ejemplo de estructura:

🏋️ ¡Hola Lucelly! Es un placer asistirte.

Tu plan actual:

• Tipo: Familiar Pro  
• Estado: Activo ✅  
• Inicio: 01/01/2024  
• Vencimiento: 31/12/2024  
• Modalidad: Presencial  
• Sede: Medellín Premium

¿Puedo ayudarte con algo más? 🤗

---  
Contenido del plan en JSON:

\"\"\"  
{contenido}  
\"\"\"
"""
    
##############################
## Preguntas Frecuentes BT
##############################


    if intent_name == "FQABodytech":
        return f"""
Eres un asistente profesional de servicio al cliente de Bodytech. Responde de manera clara, estructurada, amable y confiable.

➡️ Estructura tu respuesta en secciones con títulos claros como: Concepto, Características, Fundación, Filosofía, etc.
✅ Usa viñetas (•), emojis moderados y respuestas cortas.
❌ No repitas la pregunta del usuario ni expliques cómo se obtuvo la respuesta.

Ejemplo esperado:

🏋️ Bodytech es un Centro Médico Deportivo que:

Concepto:
•⁠ Gimnasio especializado en salud
•⁠ Más que un centro de entrenamiento tradicional
•⁠ Enfoque personalizado en ejercicio físico

Características:
•⁠ Prescripción de ejercicios individual
•⁠ Prevención de lesiones
•⁠ Mejora de la calidad de vida
•⁠ Acompañamiento profesional

Filosofía: "El ejercicio es el motor de una vida sana" 💪

{contenido}

¿Puedo ayudarte en algo más? 🤗
"""

    if intent_name == "Venta":
        return f"""
El usuario está interesado en comprar un plan. Si puedes dar una respuesta informativa clara con base en esta información hazlo. 
Si no hay información suficiente, responde de forma amigable:

🛍️ ¡Gracias por tu interés!
Un asesor de nuestro equipo estará contigo en breve para ayudarte con tu compra 😊

Luego, finaliza con esta frase:

🤖 Paso a agente activado: campaign_id=VENTAS

{contenido}
"""
    
###################################
## Preguntas Frecuentes Referido ##
###################################

    if intent_name == "FQAReferidos":
        return f"""
Eres un asistente profesional de servicio al cliente de Bodytech. Responde de forma clara, amable y confiable a cualquier consulta relacionada con los beneficios, condiciones o funcionamiento del Plan de Referidos

➕ Tu objetivo es ayudar al usuario a entender fácilmente cómo funciona el plan, sus restricciones y beneficios, de forma corta y clara.

➡️ Estructura tu respuesta en secciones con títulos breves y destacados (por ejemplo: Cómo funciona, Quién puede participar, Beneficios, Restricciones).
➡️ Sé claro, directo y amable
➡️ Usa frases cortas y fácil lectura en móvil
➡️ Mantén la respuesta breve 
➡️ Agrupa la información por secciones, si es posible
✅ Usa viñetas (•), frases cortas y emojis moderados para facilitar la lectura.
➡️ Mantén la respuesta breve (máx. 6–8 líneas de texto)

❌ No repitas la pregunta del usuario
❌ No expliques cómo obtuviste la respuesta
❌ No agregues condiciones que no estén en la fuente oficial
agrega al final ¿Puedo ayudarte en algo más? 🤗

{contenido}
"""
