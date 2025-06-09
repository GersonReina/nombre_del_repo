import json
import traceback
from utils import resumen_planes_para_bedrock, convertir_fechas_a_str, consultar_plan, responder
from utils import responder, cerrar_conversacion, mostrar_sugerencias, es_fecha_valida
from prompts import get_prompt_por_intent
from respuestas import respuesta_bedrock, obtener_respuesta_congelacion, obtener_respuesta_bedrock_desde_lex
from services import validar_documento_usuario, consultar_kb_bedrock, manejar_respuesta_post_pregunta_adicional,obtener_info_sedes
from services import validar_ciudad_usuario, obtener_id_sede, obtener_id_actividad
from redshift_utils import consultar_sedes_por_ciudad_id, consultar_clases_por_sede_id, consultar_horarios_por_sede_clase_fecha
from secret import obtener_secret

def lambda_handler(event, context):
    print("📥 Evento recibido:", json.dumps(event, indent=2))

    try:
        session_state = event.get("sessionState", {})
        intent = session_state.get("intent", {})
        intent_name = intent.get("name", "")
        session_attributes = session_state.get("sessionAttributes", {}) or {}
        input_transcript = event.get("inputTranscript", "").lower()
        slots = intent.get("slots", {})

        invocation_source = event.get("invocationSource", "")
        # -----------------------------
        # ¿Está esperando una respuesta final tipo "¿puedo ayudarte con algo más?"?
        # -----------------------------
        if session_attributes.get("esperando_respuesta_final") == "true":
            print("🔄 Procesando posible intención post-respuesta...")
            session_attributes.pop("esperando_respuesta_final", None)
            return manejar_respuesta_post_pregunta_adicional(
                input_transcript,
                session_attributes,
           )

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

    # Guarda slots/documento en sesión si existen y aún no están guardados
           if slots:
              session_attributes["slots_previos"] = json.dumps(slots)
           if session_attributes.get("document_type_id") is None and slots.get("TipoDocumento"):
              session_attributes["document_type_id"] = slots["TipoDocumento"].get("value", {}).get("interpretedValue")
           if session_attributes.get("document_number") is None and slots.get("NumeroDocumento"):
              session_attributes["document_number"] = slots["NumeroDocumento"].get("value", {}).get("interpretedValue")

    # Si nunca se mostraron las políticas, las mostramos y marcamos como mostradas
           if session_attributes.get("politicas_mostradas") != "true":
              session_attributes["politicas_mostradas"] = "true"
              return responder(
            "Bienvenid@ al Servicio al Cliente de Bodytech! "
            "Al continuar con esta comunicación estás de acuerdo con nuestra política de manejo de datos: "
            "https://bodytech.com.co/tratamiento-de-informacion\n\n¿Deseas continuar?",
            session_attributes,
            "SaludoHabeasData"
        )

    # Si ya se mostraron, procesamos la respuesta del usuario
           if any(p in input_transcript for p in ["si", "sí", "acepto", "de acuerdo", "vale", "claro", "ok", "bueno", "listo", "está bien"]):
                session_attributes["acepto_politicas"] = "true"
        # Cuando acepta, redirige a la intención original y usa los datos guardados
                return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [
                {

                    "contentType": "PlainText",
                    "content": (
                        "¡Gracias por aceptar nuestras políticas! ✅\n\n"
                        "¿En qué te puedo ayudar?\n\n"
                        "Algunas sugerencias:\n"
                        "• Consulta de información de tu plan\n"
                        "• Preguntas frecuentes sobre Bodytech\n"
                        "• Información sobre ventas y promociones\n"
                        "• Consulta de referidos"
                    )
                }
            ]
        }

           if any(p in input_transcript for p in ["no", "rechazo", "no acepto"]):
                 return cerrar_conversacion(
            "Gracias por contactarte con nosotros. Lamentablemente no podemos continuar si no aceptas nuestras políticas de tratamiento de datos.",
            "SaludoHabeasData"
        )

    # Si la respuesta no es clara, volvemos a preguntar
           return responder(
                "¿Deseas continuar y aceptar nuestras políticas de tratamiento de información?",
                session_attributes,
                "SaludoHabeasData"
            )
        # -----------------------------
        # FLUJO: ConsultarSedes
        # -----------------------------
        if intent_name == "ConsultaBedrock":
         mensaje_usuario = event["inputTranscript"]  # O el campo donde recibes el mensaje
         respuesta = obtener_respuesta_bedrock_desde_lex(event)
         return responder(respuesta, session_attributes, intent_name, fulfillment_state="Fulfilled") 

                # -----------------------------
        # FLUJO: Consultar Actividades
        # -----------------------------

        if intent_name == "ConsultaGrupales":
            try:
                print("Entrando a ConsultaGrupales")
                # Normaliza los slots
                slots = {k.lower(): v for k, v in slots.items()}
                ciudad_raw = slots.get("ciudad", {}).get("value", {}).get("interpretedValue", "")
                sede_raw = slots.get("sede", {}).get("value", {}).get("interpretedValue", "")
                clase_raw = slots.get("clase", {}).get("value", {}).get("interpretedValue", "")
                fecha = slots.get("fecha", {}).get("value", {}).get("interpretedValue", "")
                
                ciudad_raw = ciudad_raw.split(",")[0].strip()
                sede_raw = sede_raw.split(",")[0].strip()
                clase_raw = clase_raw.split(",")[0].strip()
                
                        # Si ya hay ciudad pero NO hay sede, sugiere las sedes disponibles
                if ciudad_raw and not sede_raw:
                    sedes = consultar_sedes_por_ciudad_id(ciudad_raw)
                    if sedes:
                      return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
                            "intent": intent,
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"Estas son las sedes disponibles en {ciudad_raw}: {', '.join(sedes)}. ¿Cuál deseas consultar?"
                        }],
                        "responseCard": {
                            "title": "Sedes disponibles",
                            "buttons": [{"text": s, "value": s} for s in sedes]
                        }
                    }
                    else:
                      return responder(
                        f"No se encontraron sedes para la ciudad {ciudad_raw}.",
                        session_attributes,
                        intent_name,
                        fulfillment_state="Fulfilled"
                    )

                print(f"Slots recibidos: ciudad={ciudad_raw}, sede={sede_raw}, clase={clase_raw}, fecha={fecha}")

                # Valida que todos los slots estén presentes
                if not all([ciudad_raw, sede_raw, clase_raw, fecha]):
                 return responder(
                    "Faltan datos para consultar las clases grupales. Por favor, asegúrate de indicar ciudad, sede, clase y fecha.",
                    session_attributes,
                    intent_name,
                    fulfillment_state="Fulfilled"
                )

                # Obtén los IDs
                id_sede = obtener_id_sede(sede_raw)
                id_clase = obtener_id_actividad(clase_raw)
                if not id_sede or not id_clase:
                 return responder(
                    "No se encontró la sede o clase indicada. Por favor, revisa los nombres.",
                    session_attributes,
                    intent_name,
                    fulfillment_state="Fulfilled"
                )

                # Consulta horarios
                horarios = consultar_horarios_por_sede_clase_fecha(id_sede, id_clase, fecha)
                if not horarios:
                 return responder(
                    f"No hay horarios disponibles para {clase_raw} en la sede {sede_raw} el {fecha}.",
                    session_attributes,
                    intent_name,
                    fulfillment_state="Fulfilled"
                )

                horarios_str = "\n".join(
                f"- {h['hora_inicio']} a {h['hora_fin']}" for h in horarios
                )
                mensaje = (
                f"Horarios para {clase_raw.capitalize()} en la sede {sede_raw.capitalize()} el {fecha}:\n"
                f"{horarios_str}"
                )
                return responder(mensaje, session_attributes, intent_name, fulfillment_state="Fulfilled")

            except Exception as e:
                print("❌ Error en ConsultaGrupales:", str(e))
                return responder(
                "Lo siento, ha ocurrido un error al consultar las actividades. Intenta nuevamente más tarde.",
                session_attributes,
                intent_name
                )
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
                            "content": (
                                "¡Gracias por aceptar nuestras políticas! ✅\n\n"
                                "¿En qué te podemos ayudarte?\n\n"
                                "Algunas sugerencias:\n"
                                "• Consulta de información de tu plan\n"
                                "• Preguntas frecuentes sobre Bodytech\n"
                                "• Información sobre ventas y promociones\n"
                                "• Consulta de referidos"
                            )
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
                "Bienvenid@ al Servicio al Cliente de Bodytech! "
                "Al continuar con esta comunicación estás de acuerdo con nuestra política de manejo de datos: "
                "https://bodytech.com.co/tratamiento-de-informacion\n\n¿Deseas continuar?"
            )
            return responder(mensaje, session_attributes, intent_name)
                # -----------------------------
        # FLUJO: Despedida
        # -----------------------------
        if intent_name == "Despedida":
            return cerrar_conversacion(
                "Gracias por contactarte con nosotros. ¡Feliz día! 😊",
                intent_name
            )


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
                datos_plan_str = convertir_fechas_a_str(datos_plan)

                
                if error_msg:
                    return responder(error_msg, session_attributes, intent_name)
                resumen = resumen_planes_para_bedrock(datos_plan_str)
                prompt = get_prompt_por_intent("ConsultaInfoPlan", resumen)
               
                for plan in datos_plan_str.get("data", {}).get("plans", []):
                   print(plan.get("product_name"), plan.get("date_start"), plan.get("date_end"), plan.get("line_status"))

                # 3. Guardar info del plan en sesión
                session_attributes["datos_plan_json"] = json.dumps(datos_plan_str)

                # 4. Generar respuesta usando Bedrock (sin KB)
                mensaje_final = respuesta_bedrock(intent_name, datos_plan_str)
                if not mensaje_final or not mensaje_final.strip():
                 mensaje_final = "No se encontró información de tu plan. ¿Puedo ayudarte con algo más?"
                #mensaje_final = "¡Prueba exitosa..!
                session_attributes["esperando_respuesta_final"] = "true"
                print("Mensaje final a enviar:", mensaje_final)

                return responder(mensaje_final, session_attributes, intent_name, fulfillment_state="Fulfilled")

            except Exception as e:
                print("❌ Error en ConsultaInfoPlan:", str(e))
                traceback.print_exc()
                return responder("Lo siento, ha ocurrido un error al procesar tu solicitud. Intenta nuevamente más tarde.", session_attributes, intent_name)




        # -----------------------------
        # 4️⃣ FLUJO: FQABodytech
        # -----------------------------
        if intent_name == "FQABodytech":
            try:
                config = obtener_secret("main/LexAgenteVirtualSAC")
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
                config = obtener_secret("main/LexAgenteVirtualSAC")
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
        if session_attributes.get("acepto_politicas") != "true" and intent_name == "CongelarPlan":
            print("🔁 Redirigiendo a SaludoHabeasData porque aún no se aceptan políticas.")
            return responder(
                "Para poder ayudarte con la congelación de tu plan, primero debes aceptar nuestras políticas de manejo de datos. "
                "¿Deseas continuar?",
                session_attributes,
                "SaludoHabeasData"
            )

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
                    datos_plan_str = convertir_fechas_a_str(datos_plan)

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
                config = obtener_secret("main/LexAgenteVirtualSAC")
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



    # ...otros intents...


        # -----------------------------
        # FLUJO: ConsultarSedes
        # -----------------------------
    if intent_name == "ConsultarSedes":
            respuesta = obtener_info_sedes()
            return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "intent": {
                    "name": intent_name,
                    "state": "Fulfilled"
                },
                "sessionAttributes": event.get("sessionAttributes", {})
            },
            "messages": [
                {
                    "contentType": "PlainText",
                    "content": respuesta
                }
            ]
        }
        









