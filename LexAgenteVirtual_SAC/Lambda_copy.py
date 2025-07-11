import json
import traceback
import difflib
import unicodedata
import re
from utils import resumen_planes_para_bedrock, convertir_fechas_a_str, consultar_plan, responder, respuesta_calificacion_con_botones, resumen_invitados_para_bedrock, consultar_referidos,resumen_incapacidades_para_bedrock, normalizar_fecha
from utils import responder, cerrar_conversacion, mostrar_sugerencias, obtener_resumen_grupales, responder_con_pregunta_final, consultar_invitados, consultar_incapacidades, es_fecha_valida, manejar_respuestas_info_adicional, crear_respuesta_info_adicional, terminar_sin_calificacion
from prompts import get_prompt_por_intent, get_prompt_no_info, get_prompt_info
from respuestas import respuesta_bedrock, obtener_respuesta_congelacion, consultar_bedrock_generacion
from services import validar_documento_usuario, consultar_kb_bedrock, manejar_respuesta_post_pregunta_adicional, normalizar_nombre, validar_sede_usuario, validar_clase_usuario, CATEGORIAS_CLASES
from services import validar_ciudad_usuario, obtener_id_sede, obtener_id_actividad, get_slot_value, obtener_categorias_por_linea, obtener_id_categoria_por_nombre, obtener_nombre_actividad_por_id, validar_categoria_clase_usuario
from redshift_utils import consultar_sedes_por_ciudad_id, consultar_clases_por_sede_id, consultar_horarios_por_sede_clase_fecha,consultar_sedes_por_ciudad_id_linea, armar_consulta_ingresos, ejecutar_consulta, consultar_categorias_clases_por_sede
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

        
        
        intenciones_con_documento = session_attributes.get("intenciones_con_documento", "")
        intenciones_set = set(intenciones_con_documento.split(",")) if intenciones_con_documento else set()
        flujo_activo = session_attributes.get("en_flujo_activo")
        intenciones_protegidas = [
             "ConsultaGrupales", "ConsultarInvitados", "FQAReferidos","ConsultarSedes", "FQABodytech", "Venta", 
             "ConsultaIncapacidades", "ConsultaInfoPlan", "CongelarPlan", "Ingresos"
              ]
        intenciones_que_interrumpen = [
            "FQABodytech", "Venta", "ConsultarSedes", "ConsultaGrupales",
            "ConsultarInvitados", "FQAReferidos", "ConsultaIncapacidades", 
            "ConsultaInfoPlan", "CongelarPlan", "Ingresos", "SaludoHabeasData"
        ]
        # PRIORIDAD 1: Manejar respuestas de información adicional PRIMERO
        respuesta_info = manejar_respuestas_info_adicional(session_attributes, input_transcript)
        if respuesta_info:
            return respuesta_info
            
        # PRIORIDAD 1.5: Manejar transiciones de ConsultaGrupales ANTES que esperando_respuesta_final
        if (session_attributes.get("esperando_transicion_grupales") == "true" or
            (session_attributes.get("en_flujo_activo") == "ConsultaGrupales" and
             any(re.search(pattern, input_transcript.lower()) for pattern in [
                 r'\botra sede\b', r'\botra ciudad\b', r'\botra clase\b', 
                 r'\botra categor[íi]a\b'
             ]) or input_transcript.strip() in ["1", "2", "3", "4", "5"])):
            
            # Forzar intent a ConsultaGrupales
            intent_name = "ConsultaGrupales"
            intent = {"name": "ConsultaGrupales", "slots": slots}
            print(f"🔍 Forzando procesamiento en ConsultaGrupales para transición")
        
        # PRIORIDAD 2: Manejar esperando_respuesta_final SEGUNDO (PERO excluyendo ConsultaGrupales)  
        elif (session_attributes.get("esperando_respuesta_final") == "true" and 
              not session_attributes.get("esperando_info_invitados") and
              not session_attributes.get("esperando_info_incapacidad") and 
              not session_attributes.get("esperando_info_referidos") and
              
              not session_attributes.get("esperando_transicion_grupales") and
              
              input_transcript.strip() not in ["1", "2", "3", "4", "5"]):
            
            print("🔍 ===== DEBUG RESPUESTA FINAL =====")
            print(f"🔍 input_transcript: '{input_transcript}'")
            print(f"🔍 session_attributes: {session_attributes}")
            print("🔄 Procesando posible intención post-respuesta...")
        
                
        # PRIORIDAD 3: Protección de intenciones TERCERO (después de manejar respuestas)
        if (flujo_activo and flujo_activo in intenciones_protegidas and 
            intent_name != flujo_activo and intent_name in intenciones_que_interrumpen and
            # no interrumpir si hay información adicional pendiente
            
            not any([
                session_attributes.get("esperando_info_invitados") == "true",
                session_attributes.get("esperando_info_incapacidad") == "true", 
                session_attributes.get("esperando_info_referidos") == "true"
            ])):
            respuesta_rapida = "Te ayudaré con eso después de completar tu consulta actual."
            
            if session_attributes.get("flujo_otra_sede") == "true":
                print("🔍 Permitiendo continuar flujo de otra sede...")
                session_attributes.pop("flujo_otra_sede", None)
            elif intent_name == "FQABodytech":
                respuesta_rapida = "Bodytech es un centro médico deportivo que ofrece servicios de salud y bienestar."
            elif intent_name == "Venta":
                respuesta_rapida = "Para información sobre ventas, te conectaremos con un asesor al finalizar tu consulta actual."
            elif intent_name == "ConsultarSedes":
                respuesta_rapida = "Te ayudaré con las sedes después de completar tu consulta actual."
            elif intent_name == "ConsultarInvitados":
                respuesta_rapida = "Te ayudaré con tus invitados después de completar tu consulta actual."
            elif intent_name == "ConsultaInfoPlan":
                respuesta_rapida = "Te ayudaré con la información de tu plan después de completar tu consulta actual."
            elif intent_name == "ConsultaIncapacidades":
                respuesta_rapida = "Te ayudaré con tus incapacidades después de completar tu consulta actual."
            elif intent_name == "FQAReferidos":
                respuesta_rapida = "Te ayudaré con tus referidos después de completar tu consulta actual."
            elif intent_name == "ConsultaGrupales":
                respuesta_rapida = "Te ayudaré con las clases grupales después de completar tu consulta actual."
            elif intent_name == "CongelarPlan":
                respuesta_rapida = "Te ayudaré con la congelación de tu plan después de completar tu consulta actual."
            elif intent_name == "Ingresos":
                respuesta_rapida = "Te ayudaré con la consulta de ingresos después de completar tu consulta actual."
            else:
                respuesta_rapida = "Te ayudaré con eso después de completar tu consulta actual."
            
            slots_originales = {}
            if session_attributes.get("slots_previos"):
                try:
                    slots_originales = json.loads(session_attributes["slots_previos"])
                    for slot_name, slot_value in slots_originales.items():
                        if slot_value and isinstance(slot_value, dict):
                            if "value" in slot_value and "shape" in slot_value:
                                continue 
                            elif "value" in slot_value:
                                slots_originales[slot_name] = {
                                    "value": slot_value["value"],
                                    "shape": "Scalar"
                                }
                            else:
                                valor = slot_value if isinstance(slot_value, str) else str(slot_value)
                                slots_originales[slot_name] = {
                                    "value": {
                                        "originalValue": valor,
                                        "resolvedValues": [valor],
                                        "interpretedValue": valor
                                    },
                                    "shape": "Scalar"
                                }
                except Exception as e:
                    print("❌ Error reconstruyendo slots:", str(e))
                    slots_originales = {} 
            # Determinar mensaje de continuación según el flujo activo
            if flujo_activo == "ConsultaGrupales":
                ciudad_actual = get_slot_value(slots_originales, "ciudad") or session_attributes.get("ciudad")
                sede_actual = get_slot_value(slots_originales, "sede")     
                clase_actual = get_slot_value(slots_originales, "clase")   
                fecha_actual = get_slot_value(slots_originales, "fecha")
                
                print("🔍 DEBUG slots actuales:", slots)
                print("🔍 DEBUG slots_originales:", slots_originales)
                print("🔍 DEBUG session_attributes ciudad:", session_attributes.get("ciudad"))
                if not ciudad_actual:
                    slot_to_elicit = "ciudad"
                    mensaje_continuacion = "Continuemos con la consulta de tus clases grupales. ¿En qué ciudad te encuentras?"
                elif not sede_actual:
                    slot_to_elicit = "sede"
                    mensaje_continuacion = f"Continuemos con la consulta de clases grupales en {ciudad_actual}. ¿En qué sede deseas consultar?"
                elif not clase_actual:
                    slot_to_elicit = "clase"
                    mensaje_continuacion = f"Continuemos con la consulta de clases grupales en la sede {sede_actual}. ¿Qué clase deseas consultar?"
                elif not fecha_actual:
                    slot_to_elicit = "fecha"
                    mensaje_continuacion = f"Continuemos con la consulta de {clase_actual} en {sede_actual}. ¿Para qué fecha deseas consultar los horarios?"
                else:
                    slot_to_elicit = None
                    mensaje_continuacion = "Continuemos con la consulta de tus clases grupales."
                return {
                    "sessionState": {
                        "dialogAction": {
                            "type": "ElicitSlot" if slot_to_elicit else "ElicitIntent",
                            "slotToElicit": slot_to_elicit
                        } if slot_to_elicit else {"type": "ElicitIntent"},
                        "intent": {
                            "name": flujo_activo,
                            "slots": slots_originales,
                            "state": "InProgress"
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": f"{respuesta_rapida} {mensaje_continuacion}"
                    }]
                }
            elif flujo_activo == "ConsultarInvitados":
                mensaje_continuacion = "Continuemos consultando tus invitados."
            elif flujo_activo == "FQAReferidos":
                mensaje_continuacion = "Continuemos consultando tus referidos."
            elif flujo_activo == "ConsultaIncapacidades":
                mensaje_continuacion = "Continuemos consultando tus incapacidades."
            elif flujo_activo == "ConsultaInfoPlan":
                mensaje_continuacion = "Continuemos consultando la información de tu plan."
            elif flujo_activo == "CongelarPlan":
                mensaje_continuacion = "Continuemos con la consulta para congelar tu plan."
            elif flujo_activo == "Ingresos":
                mensaje_continuacion = "Continuemos con la consulta de ingresos."
            else:
                mensaje_continuacion = "Continuemos con tu consulta actual."
        
            return {
                "sessionState": {
                    "dialogAction": {"type": "ElicitIntent"},
                    "intent": {
                        "name": flujo_activo,
                        "slots": slots_originales,  # mantiene los slots actuales
                        "state": "InProgress"  # mantiene la intención activa
                    },
                    "sessionAttributes": session_attributes
                },
                "messages": [{
                    "contentType": "PlainText",
                    "content": f"{respuesta_rapida} {mensaje_continuacion}"
                }]
            }
            
            input_normalizado = unicodedata.normalize('NFKD', input_transcript.lower()).encode('ascii', 'ignore').decode('ascii')
            input_normalizado = input_normalizado.encode('ascii', 'ignore').decode('ascii')
            input_normalizado = re.sub(r'[^\w\s]', '', input_normalizado)
            
            mapeo_respuestas = {
                's': 'si',
                'sued': 'si',
                'sud': 'si',
                'sd': 'si'
            }
            if input_normalizado.strip() in mapeo_respuestas:
                input_normalizado = mapeo_respuestas[input_normalizado.strip()]
            
            print(f"🔍 input_normalizado: '{input_normalizado}'")
            
            if any(p in input_normalizado for p in ["no", "nada", "gracias", "eso es todo", "ninguna", "no gracias"]):
                print("🔍 Usuario dijo NO - enviando a calificación")
                session_attributes.pop("esperando_respuesta_final", None)
                
                session_attributes.pop("en_flujo_activo", None)
                session_attributes.pop("categoria_clase_preguntada", None)  
                session_attributes.pop("clase_display", None)
                session_attributes.pop("slots_previos", None)
                session_attributes.pop("ciudad_nombre", None)
                session_attributes.pop("sede_nombre", None)
                session_attributes.pop("ciudad_id", None)
                session_attributes.pop("sede_id", None)
                session_attributes["esperando_calificacion"] = "true"
                return respuesta_calificacion_con_botones(session_attributes)
            
            elif any(p in input_normalizado for p in ["si", "yes", "claro", "vale", "ok", "por supuesto"]):
                print("🔍 Usuario dijo SÍ - limpiando sesión y continuando")
                ultimo_intent = session_attributes.get("ultimo_intent_completado")
                
                if ultimo_intent == "ConsultaGrupales":
                    print("🔍 Usuario quiere consultar otro horario - redirigiendo a ConsultaGrupales")
                
                    session_attributes.pop("esperando_respuesta_final", None)
                    session_attributes.pop("en_flujo_activo", None)
                    session_attributes.pop("categoria_clase_preguntada", None)  
                    session_attributes.pop("clase_display", None)
                    session_attributes.pop("slots_previos", None)
                    # MANTENER sede y ciudad si las tenía
                    
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitIntent"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                "¡Perfecto! 🏃‍♂️ ¿Qué tipo de consulta deseas hacer?\n\n"
                                
                                "📍 **Opciones disponibles:**\n\n"
                                
                                "🌎 **'Otra ciudad'** - Para consultar clases en una ciudad diferente\n"
                                "   ▸ Te preguntaré por la nueva ciudad\n\n"
                                
                                "🏢 **'Otra sede'** - Para consultar en otra sede de la misma ciudad\n"
                                "   ▸ Mantengo la ciudad actual y te muestro otras sedes\n\n"
                                
                                "🏃‍♂️ **'Otra clase'** - Para consultar otra clase en la misma sede\n"
                                "   ▸ Mantengo la sede actual y te muestro otras clases\n\n"
                                
                                "💬 **Escribe tu opción:**"
                            )
                        }]
                    }
                else:
                    # Flujo normal para otras intenciones
                    session_attributes.pop("esperando_respuesta_final", None)
                    session_attributes.pop("en_flujo_activo", None)
                    session_attributes.pop("categoria_clase_preguntada", None)  
                    session_attributes.pop("clase_display", None)
                    session_attributes.pop("slots_previos", None)
                    session_attributes.pop("sede_nombre", None)
                    session_attributes.pop("ciudad", None)
                
                print(f"🔍 session_attributes después de limpiar: {session_attributes}")
                print("🔍 ===== FIN DEBUG RESPUESTA FINAL =====")
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            "¡Perfecto! 😊 ¿En qué más puedo ayudarte?\n\n"
                            "Algunas opciones:\n"
                            "📄 Preguntas frecuentes sobre Bodytech\n"
                            "🏢 Consultar sedes y horarios\n"
                            "🏃‍♂️ Clases grupales disponibles\n"
                            "📅 Información de tu plan\n"
                            "👥 Consultar invitados\n"
                            "🏆 Información sobre referidos\n"
                            "🧾 Consultar incapacidades\n"
                            "🛍️ Información de ventas\n\n"
                            "¿Sobre qué tema te gustaría que te ayude?"
                        )
                    }]
                }
            else:
                print("🔍 Usuario respondió algo que no es sí/no - verificando transición...")
                ultimo_intent = session_attributes.get("ultimo_intent_completado")
                
                
            try:
                    prompt = f"""
Usuario dijo: "{input_transcript}"

Clasifica este mensaje en una de estas intenciones válidas:
- FQABodytech (preguntas sobre Bodytech)
- Venta (información de ventas)
- ConsultaInfoPlan (información del plan del usuario)
- ConsultarInvitados (consultar invitados)
- ConsultaIncapacidades (consultar incapacidades)
- FQAReferidos (consultar referidos)
- ConsultaGrupales (clases grupales)
- ConsultarSedes (consultar sedes)
- CongelarPlan (congelar plan)

Si la frase NO es claramente una de estas intenciones, responde: "No puedo ayudarte con eso, pero puedo ayudarte con otras cosas como preguntas frecuentes, información de tu plan, clases grupales, etc."

"""
                    
                    intencion_detectada = consultar_bedrock_generacion(prompt).strip()
                    
                    intenciones_validas = [
                        "FQABodytech", "Venta", "ConsultaInfoPlan", "ConsultarInvitados", 
                        "ConsultaIncapacidades", "FQAReferidos", "ConsultaGrupales", 
                        "ConsultarSedes", "CongelarPlan"
                    ]
                    
                    if intencion_detectada in intenciones_validas:
                        print(f"Intención detectada: {intencion_detectada}")
                        
                        # Limpiar sesión y disparar la nueva intención
                        keys_to_remove = [
                            "esperando_respuesta_final", "en_flujo_activo", "categoria_clase_preguntada",
                            "clase_display", "slots_previos", "sede_nombre", "ciudad"
                        ]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitIntent"},
                                "intent": {
                                    "name": intencion_detectada,
                                    "state": "InProgress",
                                    "slots": {}
                                },
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": f"¡Perfecto! Te ayudo con {intencion_detectada.replace('FQA', '').replace('Consulta', 'consultar ')}"
                            }]
                        }
                    else:
                        print(f"❌ No se detectó intención válida: {intencion_detectada}")
                        raise Exception("No es una intención válida")
                        
            except Exception as e:
                    print(f"⚠️ Error en clasificación o no es intención: {str(e)}")
                    
                    # Si no es una intención válida, responder que no se entendió
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitIntent"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                "No logré identificar tu solicitud 🤔\n\n"
                                "¿Puedes ser más específico sobre lo que necesitas?\n\n"
                                "Puedo ayudarte con:\n"
                                "📄 Preguntas frecuentes sobre Bodytech\n"
                                "🏢 Consultar sedes y horarios\n"
                                "🏃‍♂️ Clases grupales disponibles\n"
                                "📅 Información de tu plan\n"
                                "👥 Consultar invitados\n"
                                "🏆 Información sobre referidos\n"
                                "🧾 Consultar incapacidades\n"
                                "🛍️ Información de ventas\n\n"
                                "¿Sobre cuál tema necesitas ayuda?"
                            )
                        }]
                    }
             
        intents_requieren_doc = {"ConsultaInfoPlan", "ConsultarInvitados", "ConsultaIncapacidades", "FQAReferidos"}
        if intent_name in intents_requieren_doc:
            intenciones_set.add(intent_name)
            session_attributes["intenciones_con_documento"] = ",".join(intenciones_set)

            # Si ya pasó por 2 o más intenciones de este tipo
            if (
                len(intenciones_set) > 1
                and not session_attributes.get("preguntando_otro_documento")
                and not session_attributes.get("cambiando_documento")
            ):
                session_attributes["preguntando_otro_documento"] = "true"
                session_attributes["cambiando_documento"] = ""
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "cambiar_documento"},
                        "intent": {
                            "name": intent_name,
                            "state": "InProgress"
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿Quieres consultar con otro documento o seguir usando el que ya indicaste?"
                    }]
    }
        # Si el usuario responde explícitamente que quiere cambiar de documento:
        if slots and slots.get("cambiar_documento"):
            valor = slots["cambiar_documento"].get("value", {}).get("interpretedValue", "").lower()
            if "otro" in valor:
                # Limpia los datos de documento en sesión
                session_attributes.pop("document_type_id", None)
                session_attributes.pop("document_type_raw", None)
                session_attributes.pop("document_number", None)
                session_attributes["preguntando_otro_documento"] = ""
                session_attributes["cambiando_documento"] = "true" 
                intent["slots"] = {
                    "document_type": None,
                    "document_number": None,
                    "cambiar_documento": None
                }
                _, _, session_attributes, respuesta_incompleta = validar_documento_usuario(
                intent["slots"],  # slots vacíos para forzar la recolección
                session_attributes,
                "",  # input_transcript vacío
                intent
            )
                return respuesta_incompleta
            elif "mismo" in valor:
                session_attributes["preguntando_otro_documento"] = ""
                session_attributes["cambiando_documento"] = ""
                      
        if session_attributes.get("esperando_calificacion") == "true":
            session_attributes.pop("esperando_calificacion", None)
            return {
                        "sessionState": {
                            "dialogAction": {"type": "Close"},
                            "intent": {
                                "name": intent_name if intent_name else "CalificacionServicio",
                                "state": "Fulfilled"
                            },
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": "¡Gracias por tu calificación! Que tengas un excelente día. 😊"
                        }]
                    }
        
        if (
            session_attributes.get("slots_previos")
            and input_transcript.strip()
        ):
            try:
                prev_slots = json.loads(session_attributes["slots_previos"])
                clases_previas = []
                if "clase" in prev_slots and "resolvedValues" in prev_slots["clase"]:
                    # Soporta resolvedValues como lista de strings o lista de listas
                    for val in prev_slots["clase"]["resolvedValues"]:
                        if isinstance(val, str):
                            clases_previas.extend([v.strip().lower() for v in val.split(",")])
                # Si el input coincide con alguna clase sugerida, fuerza el intent
                if input_transcript.strip().lower() in clases_previas:
                    prev_slots["clase"]["value"] = {
                        "originalValue": input_transcript,
                        "resolvedValues": [input_transcript],
                        "interpretedValue": input_transcript
                    }
                    # Forzar el intent y los slots
                    intent = {"name": "ConsultaGrupales", "slots": prev_slots}
                    intent_name = "ConsultaGrupales"
                    slots = prev_slots
                    print("🔄 Redirigiendo a ConsultaGrupales with slots reconstruidos:", slots)
            except Exception as e:
                print("⚠️ Error reconstruyendo slots para ConsultaGrupales:", str(e))
        invocation_source = event.get("invocationSource", "")
        # -----------------------------
        # ¿Está esperando una respuesta final tipo "¿puedo ayudarte con algo más?"?
        # -----------------------------
       # if session_attributes.get("esperando_respuesta_final") == "true":
        #    print("🔄 Procesando posible intención post-respuesta...")
         #   session_attributes.pop("esperando_respuesta_final", None)
          #  return manejar_respuesta_post_pregunta_adicional(
           #     input_transcript,
            #    session_attributes,
           #)

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
            rechazos = [
                "no", "rechazo", "no acepto", "no deseo continuar", 
                "no quiero continuar", "no deseo", "no quiero",
                "decline", "rechazar", "olvidalo", "claro que no", 
                "por supuesto que no", "no quiero nada"
            ]
            
            if any(palabra in input_transcript for palabra in rechazos):
                print("🚫 Rechazo detectado - terminando sin calificación")
                return terminar_sin_calificacion(
                    "Gracias por contactarte con nosotros. Lamentablemente no podemos continuar si no aceptas nuestras políticas de tratamiento de datos.",
                    session_attributes
                )
            
            # Para cualquier otra intención, redirigir a SaludoHabeasData
            # Preservar información si existe
            if slots:
                session_attributes["slots_previos"] = json.dumps(slots)
            if session_attributes.get("document_type_id") is None and slots.get("TipoDocumento"):
                session_attributes["document_type_id"] = slots["TipoDocumento"].get("value", {}).get("interpretedValue")
            if session_attributes.get("document_number") is None and slots.get("NumeroDocumento"):
                session_attributes["document_number"] = slots["NumeroDocumento"].get("value", {}).get("interpretedValue")
            
            # Forzar intent a SaludoHabeasData
            intent_name = "SaludoHabeasData"
            intent = {"name": "SaludoHabeasData", "slots": {}}
            
            print("🔄 Intent forzado a SaludoHabeasData para manejar políticas")

    # Si nunca se mostraron las políticas, las mostramos y marcamos como mostradas
            if session_attributes.get("politicas_mostradas") != "true":
              session_attributes["politicas_mostradas"] = "true"
              return responder(
            "Bienvenid@ al Servicio al Cliente de Bodytech soy Milo tu asistente virtual! "
            "Al continuar con esta comunicación estás de acuerdo con nuestra política de manejo de datos: "
            "https://bodytech.com.co/tratamiento-de-informacion\n\n¿Deseas continuar?",
            session_attributes,
            "SaludoHabeasData"
        )

    # Si ya se mostraron, procesamos la respuesta del usuario
            if any(p in input_transcript for p in ["si", "sí", "acepto", "de acuerdo", "vale", "claro", "ok", "bueno", "listo", "está bien","por supuesto"]):
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
                        "¿En qué puedo ayudarte?\n\n"
                        "Algunas opciones:\n"
                            "📄 Preguntas frecuentes sobre Bodytech\n"
                            "🏢 Consultar sedes y horarios\n"
                            "🏃‍♂️ Clases grupales disponibles\n"
                            "📅 Información de tu plan\n"
                            "👥 Consultar invitados\n"
                            "🏆 Información sobre referidos\n"
                            "🧾 Consultar incapacidades\n"
                            "🛍️ Información de ventas\n\n"
                            "¿Sobre qué tema te gustaría que te ayude?"
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
        # FLUJO: Consultar Actividades
        # -----------------------------

        if intent_name == "ConsultaGrupales":
            try:
                if session_attributes.get("flujo_otra_sede") == "true":
                    print("🔍 Continuando flujo de otra sede...")
                    session_attributes.pop("flujo_otra_sede", None)
                session_attributes["en_flujo_activo"] = intent_name
                
                #Manejar transiciones primero
                tipo_transicion = get_slot_value(slots, "tipo_transicion")
                
                # Detectar transición desde input_transcript (sin restricciones)
                if not tipo_transicion and input_transcript:
                    input_lower = input_transcript.lower().strip()
                    print(f"🔍 Verificando transición desde input: '{input_lower}'")
                    
                    # Usar regex para coincidencias exactas de palabras
                    if re.search(r'\botra sede\b', input_lower) or input_lower == "2":
                        tipo_transicion = "otra_sede"
                        print(f"🔍 Transición detectada: OTRA SEDE")
                    elif re.search(r'\botra ciudad\b', input_lower) or input_lower == "1":
                        tipo_transicion = "otra_ciudad"
                        print(f"🔍 Transición detectada: OTRA CIUDAD")
                    elif re.search(r'\botra clase\b', input_lower) or input_lower == "3":
                        tipo_transicion = "otra_clase"
                        print(f"🔍 Transición detectada: OTRA CLASE")
                    elif re.search(r'\botra categor[íi]a\b', input_lower) or input_lower == "4":
                        tipo_transicion = "otra_categoria"
                        print(f"🔍 Transición detectada: OTRA CATEGORÍA")
                    elif re.search(r'\bno\b', input_lower) or re.search(r'\bno gracias\b', input_lower) or input_lower == "5":
                        tipo_transicion = "no"
                        print(f"🔍 Transición detectada: NO")
                    elif session_attributes.get("esperando_transicion_grupales") == "true":
                        # Solo mostrar error si estamos esperando transición específicamente
                        print(f"🔍 Respuesta inválida para transición: '{input_lower}'")
                        session_attributes.pop("esperando_transicion_grupales", None)
                        
                        contenido = (
                            "🤔 No entendí tu respuesta. Por favor, selecciona una opción válida:\n\n"
                            "1️⃣ Otra ciudad\n"
                            "2️⃣ Otra sede\n"
                            "3️⃣ Otra clase\n"
                            "4️⃣ Otra categoría\n"
                            "5️⃣ No gracias\n\n"
                            "Responde con el número (1, 2, 3, 4 o 5) o escribe directamente lo que deseas:"
                        )
                        
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                                "intent": {
                                    "name": "ConsultaGrupales", 
                                    "state": "InProgress",
                                    "slots": {}
                                },
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": contenido
                            }]
                        }
                
                # Si hay transición, procesarla
                if tipo_transicion:
                    print(f"🔍 Tipo de transición detectado: {tipo_transicion}")
                    
                    # Limpiar banderas de transición
                    session_attributes.pop("esperando_transicion_grupales", None)
                    
                    # Limpiar el slot de transición para evitar bucles
                    if "tipo_transicion" in slots:
                        slots["tipo_transicion"] = None
                    
                    # Procesar transición
                    if tipo_transicion == "otra_sede":
                        print("✅ Transición: OTRA SEDE")
                        ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
                        
                        if not ciudad_actual:
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                                    "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad deseas consultar otra sede?"}]
                            }
                        
                        # Limpiar solo sede y clase, mantener ciudad
                        keys_to_remove = ["sede_nombre", "sede_id", "categoria_clase_preguntada", "clase_display", "slots_previos"]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        slots_nuevos = {
                            "ciudad": {
                                "value": {
                                    "originalValue": ciudad_actual,
                                    "resolvedValues": [ciudad_actual],
                                    "interpretedValue": ciudad_actual
                                },
                                "shape": "Scalar"
                            }
                        }
                        
                        slots = slots_nuevos
                        print(f"✅ Slots configurados para otra sede: {slots}")
                    
                    elif tipo_transicion == "otra_ciudad":
                        print("✅ Transición: OTRA CIUDAD")
                        
                        # Limpiar toda la información geográfica
                        keys_to_remove = [
                            "categoria_clase_preguntada", "clase_display", "slots_previos",
                            "sede_nombre", "sede_id", "ciudad_nombre", "ciudad_id", "ciudad"
                        ]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        # Empezar desde cero - NO configurar slots de ciudad
                        slots = {}
                        print("✅ Iniciando consulta en nueva ciudad - slots vacíos")
                        
                    elif tipo_transicion == "otra_clase":
                        print("✅ Transición: OTRA CLASE")
                        ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
                        sede_actual = session_attributes.get("sede_nombre")
                        
                        if not ciudad_actual or not sede_actual:
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                                    "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad y sede deseas consultar otra clase?"}]
                            }
                        
                        # Limpiar solo clase info, mantener ciudad y sede
                        keys_to_remove = ["categoria_clase_preguntada", "clase_display", "slots_previos"]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        slots_nuevos = {
                            "ciudad": {
                                "value": {"originalValue": ciudad_actual, "resolvedValues": [ciudad_actual], "interpretedValue": ciudad_actual},
                                "shape": "Scalar"
                            },
                            "sede": {
                                "value": {"originalValue": sede_actual, "resolvedValues": [sede_actual], "interpretedValue": sede_actual},
                                "shape": "Scalar"
                            }
                        }
                        
                        slots = slots_nuevos
                        print(f"✅ Slots configurados para otra clase: {slots}")
                        
                    elif tipo_transicion == "otra_categoria":
                        print("✅ Transición: OTRA CATEGORÍA")
                        ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
                        sede_actual = session_attributes.get("sede_nombre")
                        
                        if not ciudad_actual or not sede_actual:
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                                    "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad y sede deseas consultar por categoría?"}]
                            }
                        
                        slots_nuevos = {
                            "ciudad": {
                                "value": {"originalValue": ciudad_actual, "resolvedValues": [ciudad_actual], "interpretedValue": ciudad_actual},
                                "shape": "Scalar"
                            },
                            "sede": {
                                "value": {"originalValue": sede_actual, "resolvedValues": [sede_actual], "interpretedValue": sede_actual},
                                "shape": "Scalar"
                            }
                        }
                        
                        # Reset categoría para mostrar opciones nuevamente
                        session_attributes.pop("categoria_clase_preguntada", None)
                        
                        slots = slots_nuevos
                        print(f"✅ Slots configurados para otra categoría: {slots}")
                        
                    elif tipo_transicion == "no":
                        print("✅ Usuario no desea más consultas")
                        # Limpiar todo y enviar a calificación
                        keys_to_remove = [
                            "en_flujo_activo", "categoria_clase_preguntada", "clase_display", "slots_previos",
                            "ciudad_nombre", "sede_nombre", "ciudad_id", "sede_id"
                        ]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        session_attributes["esperando_calificacion"] = "true"
                        return respuesta_calificacion_con_botones(session_attributes)
                
                # CONTINUAR CON EL FLUJO NORMAL DE CONSULTAGRUPALES
                print("🔄 Continuando con flujo normal de ConsultaGrupales...")
                
                # Manejar slots previos si es necesario
                
                if (slots and len([k for k, v in slots.items() if v and v.get("value", {}).get("interpretedValue")]) == 1
                    and "clase" in slots and session_attributes.get("slots_previos")):
                    prev_slots = json.loads(session_attributes["slots_previos"])
                    prev_slots["clase"] = slots["clase"]
                    slots = prev_slots
                
                print("Entrando a ConsultaGrupales")
                slots = {k.lower(): v for k, v in slots.items()}
                
                # Extraer valores de slots
                ciudad_raw = get_slot_value(slots, "ciudad")
                sede_raw = get_slot_value(slots, "sede")
                clase_raw = get_slot_value(slots, "clase")
                fecha = get_slot_value(slots, "fecha")

                # Limpiar comas de ciudad
                if ciudad_raw and "," in ciudad_raw:
                    ciudad_raw = ciudad_raw.split(",")[0].strip().lower()
                    
                # 1. VALIDAR CIUDAD
                ciudad_id, ciudad_nombre, session_attributes, respuesta_ciudad = validar_ciudad_usuario(
                    slots, session_attributes, input_transcript, intent
                )
                if respuesta_ciudad:
                    return respuesta_ciudad
                    
                # Guardar slots y ciudad en sesión
                slots_para_guardar = {k: v for k, v in slots.items() if v}
                session_attributes["slots_previos"] = json.dumps(slots_para_guardar)
                session_attributes["ciudad"] = ciudad_nombre

                # 2. VALIDAR SEDE (si existe)
                if sede_raw or (slots.get("sede") and slots["sede"].get("value")):
                    print(f"🔍 DEBUG SEDE - sede_raw antes de validar: '{sede_raw}'")
                    print(f"🔍 DEBUG SEDE - ciudad_id: {ciudad_id}")
                    print(f"🔍 DEBUG SEDE - input_transcript: '{input_transcript}'")
                    
                    if not sede_raw and slots.get("sede"):
                        sede_slot = slots["sede"]
                        if sede_slot and isinstance(sede_slot, dict):
                            sede_raw = (
                                sede_slot.get("value", {}).get("interpretedValue")
                                or sede_slot.get("value", {}).get("originalValue")
                                or ""
                            )
                            print(f"🔍 DEBUG SEDE - sede_raw extraído del slot: '{sede_raw}'")
                    
                    sede_id, sede_nombre, session_attributes, respuesta_sede = validar_sede_usuario(
                        slots, session_attributes, input_transcript, intent, ciudad_id
                    )
                    
                    print(f"🔍 DEBUG SEDE - Resultado validación:")
                    print(f"🔍   - sede_id: {sede_id}")
                    print(f"🔍   - sede_nombre: '{sede_nombre}'")
                    print(f"🔍   - respuesta_sede: {bool(respuesta_sede)}")
                    
                    if respuesta_sede:
                        print("🔍 DEBUG SEDE - Retornando respuesta_sede")
                        return respuesta_sede
                    
                    if sede_id and sede_nombre:
                        print(f"🔍 DEBUG SEDE - Actualizando slots con sede validada")
                        
                        # Actualizar el slot de sede en el intent
                        intent["slots"]["sede"] = {
                            "value": {
                                "originalValue": sede_nombre,
                                "resolvedValues": [sede_nombre],
                                "interpretedValue": sede_nombre
                            },
                            "shape": "Scalar"
                        }
                        
                        # Actualizar slots locales
                        slots["sede"] = intent["slots"]["sede"]
                        sede_raw = sede_nombre.lower()
                        
                        session_attributes["sede_nombre"] = sede_nombre
                        session_attributes["sede_id"] = str(sede_id)
                        
                        print(f"🔍 DEBUG SEDE - sede_raw actualizada a: '{sede_raw}'")
                        print(f"🔍 DEBUG SEDE - slots['sede'] actualizado: {slots['sede']}")
                        print(f"🔍 DEBUG SEDE - session_attributes actualizados")
                        
                        input_transcript = ""
                    
                    elif sede_nombre:
                        sede_raw = sede_nombre.lower()
                        session_attributes["sede_nombre"] = sede_nombre
                        print(f"🔍 DEBUG SEDE - sede_raw corregida a: '{sede_raw}'")

                # Limpiar comas de sede y clase
                if sede_raw and "," in sede_raw:
                    sede_raw = sede_raw.split(",")[0].strip()
                if clase_raw and "," in clase_raw:
                    clase_raw = clase_raw.split(",")[0].strip()

                # 3. ELICITAR SEDE si no está presente
                if ciudad_raw and not sede_raw:
                    print(f"🔍 ===== CONSULTANDO SEDES =====")
                    print(f"🔍 ciudad_id: {ciudad_id}")
                    print(f"🔍 Llamando consultar_sedes_por_ciudad_id({ciudad_id}, 1)")
                    
                    try:
                        sedes = consultar_sedes_por_ciudad_id(ciudad_id, 1)
                        print(f"🔍 Sedes obtenidas: {sedes}")
                        print(f"🔍 Tipo de sedes: {type(sedes)}")
                        print(f"🔍 Longitud de sedes: {len(sedes) if sedes else 0}")
                        
                        sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                        print(f"🔍 sedes_nombres: {sedes_nombres}")
                        
                        if not sedes_nombres:
                            print(f"❌ No se encontraron sedes para ciudad_id: {ciudad_id}")
                            return responder(f"No se encontraron sedes para la ciudad {ciudad_nombre}.", 
                                           session_attributes, intent_name, fulfillment_state="Fulfilled")
                        
                        print(f"🔍 ===== CONSTRUYENDO RESPUESTA SEDES =====")
                        respuesta_sedes = {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
                                "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": f"📍 **Sedes disponibles en {ciudad_nombre}:**\n\n"
                                    + "\n".join(f"- {sede}" for sede in sedes_nombres)
                                    + f"\n\n💬 ¿En cuál sede deseas consultar las clases grupales?"
                                    if sedes_nombres else f"No se encontraron sedes en {ciudad_nombre}."
                                    
                            }]
                        }
                        
                        print(f"🔍 ===== RETORNANDO RESPUESTA SEDES =====")
                        print(f"🔍 Respuesta construida: {respuesta_sedes}")
                        return respuesta_sedes
                        
                    except Exception as e:
                        print(f"❌ ERROR en consulta de sedes: {str(e)}")
                        print(f"❌ Error tipo: {type(e)}")
                        import traceback
                        print(f"❌ Traceback: {traceback.format_exc()}")
                        
                        # Continuar con el flujo en lugar de fallar
                        print(f"🔄 Continuando flujo a pesar del error en sedes...")
                        sede_raw = "centro mayor"  # Sede por defecto para continuar
                        print(f"🔄 Usando sede por defecto: '{sede_raw}'")

                # 4. ELICITAR CLASE CON OPCIÓN DE CATEGORÍAS
                if sede_raw and not clase_raw:
                    print(f"🔍 ===== PASO 4A: VERIFICACIÓN =====")
                    print(f"🔍 sede_raw: '{sede_raw}' (tipo: {type(sede_raw)})")
                    print(f"🔍 clase_raw: '{clase_raw}' (tipo: {type(clase_raw)})")
                    print(f"🔍 Condición sede_raw and not clase_raw: {sede_raw and not clase_raw}")
                    print(f"🔍 slots completos: {slots}")
                    print(f"🔍 session_attributes: {session_attributes}")
                    print(f"🔍 ==============================")
                    
                    id_sede = obtener_id_sede(sede_raw)
                    print(f"🔍 id_sede obtenido: {id_sede}")
                    
                    if not id_sede:
                        print(f"❌ No se pudo obtener id_sede para: '{sede_raw}'")
                        return responder("No se pudo identificar la sede seleccionada.", 
                                       session_attributes, intent_name, fulfillment_state="Fulfilled")
                    
                    # Verificar si ya se preguntó por categorías
                    categoria_clase_preguntada = session_attributes.get("categoria_clase_preguntada")
                    categoria_clase_seleccionada = get_slot_value(slots, "categoria_clase")
                    confirmar_categoria = get_slot_value(slots, "confirmar_mostrar_sedes")
                    
                    print(f"🔍 categoria_clase_preguntada: {categoria_clase_preguntada}")
                    print(f"🔍 categoria_clase_seleccionada: {categoria_clase_seleccionada}")
                    print(f"🔍 confirmar_categoria: {confirmar_categoria}")
                    
                    # PASO 4A: Preguntar si quiere ver por categorías (solo la primera vez)
                    print(f"🔍 Verificando condición: not categoria_clase_preguntada = {not categoria_clase_preguntada}")
                    if not categoria_clase_preguntada:
                        print("🔍 EJECUTANDO PASO 4A: Preguntando por categorías...")
                        session_attributes["categoria_clase_preguntada"] = "si"
                        
                        # Verificar construcción de respuesta
                        respuesta_4a = {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                                "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    "¿Cómo prefieres ver las clases disponibles? 🤔\n\n"
                                    
                                    "🎯 **'Categoría'** - Te muestro las clases organizadas por tipo:\n"
                                    "   • Cardio y Ritmos \n"
                                    "   • Cuerpo y Mente \n"
                                    "   • Fuerza y Tono \n"
                                    "   • Indoor Cycling \n"
                                    "   • Otros (Ejemplo: Zonas Humedas)\n\n"
                                    
                                    "📋 **'Ver todas'** - Te muestro todas las clases de una vez\n\n"
                                    
                                    "💬 **Ejemplos de respuesta:**\n"
                                    "🔸 'Categoría' para navegación organizada\n"
                                    "🔸 'Ver todas' para lista completa"
                                )
                            }]
                        }
                        
                        print(f"🔍 DEBUG PASO 4A - Respuesta construida:")
                        print(f"🔍   - sessionState: {respuesta_4a['sessionState']}")
                        print(f"🔍   - messages: {respuesta_4a['messages']}")
                        print(f"🔍 RETORNANDO RESPUESTA PASO 4A...")
                        
                        return respuesta_4a
                    # PASO 4C: Mostrar clases de la categoría seleccionada
                    categoria_clase_seleccionada = get_slot_value(slots, "categoria_clase")
                    if categoria_clase_seleccionada:
                        print(f"🔍 EJECUTANDO PASO 4C: Mostrando clases de categoría '{categoria_clase_seleccionada}'")
                        categoria_validada, mensaje_error = validar_categoria_clase_usuario(categoria_clase_seleccionada)
                        
                        if not categoria_validada:
                            print(f"❌ Categoría no válida: '{categoria_clase_seleccionada}'")
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "categoria_clase"},
                                    "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": f"{mensaje_error}\n\n🎯 **Selecciona tu categoría favorita:**\n\n💃 **Cardio y Ritmos** 🎵\n🧘 **Cuerpo y Mente** ☯️\n💪 **Fuerza y Tono** 🏋️‍♀️\n🚴 **Indoor Cycling** ⚡\n📚 **Otros** ✨\n\n💬 **Escribe el nombre de la categoría:**"
                                }]
                            }
                        
                        print(f"✅ Categoría validada: '{categoria_validada}'")
                        
                        # Consultar clases de la categoría específica
                        clases = consultar_clases_por_sede_id(id_sede, categoria_validada)
                        clases_nombres = [c['clase'] for c in clases] if clases and isinstance(clases[0], dict) else clases
                        
                        if not clases_nombres:
                            print(f"❌ No se encontraron clases para categoría '{categoria_validada}'")
                            return responder(f"No se encontraron clases de la categoría '{categoria_validada}' en la sede {sede_raw}.", 
                                           session_attributes, intent_name, fulfillment_state="Fulfilled")
                        
                        print(f"✅ Clases encontradas: {clases_nombres}")
                        sede_mostrar = session_attributes.get("sede_nombre", sede_raw.title())
                        
                        # Limpiar el slot categoria_clase para evitar bulces
                        intent["slots"]["categoria_clase"] = None
                        session_attributes.pop("categoria_clase_seleccionada", None)
                        
                        print(f"🔄 Limpiando slot categoria_clase y forzando elicitación de 'clase'")
                        print(f"🔄 Clases disponibles: {clases_nombres}")
                        
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "clase"},
                                "intent": {
                                    "name": intent["name"], 
                                    "slots": intent["slots"],
                                    "state": "InProgress"
                                },
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": f"📝 **Clases de {categoria_validada.title()} disponibles en {sede_mostrar}:**\n\n"
                                    + "\n".join(f"- {clase}" for clase in clases_nombres)
                                    if clases_nombres else f"No se encontraron clases de {categoria_validada} en {sede_mostrar}."
                                    + "\n\n💬 ¿Cuál clase deseas consultar?"
                            }]
                        }

                    # PASO 4B: Procesar respuesta de categorías (SEGUNDO - solo si no hay categoria_clase)
                    confirmar_categoria = get_slot_value(slots, "confirmar_mostrar_sedes")
                    if confirmar_categoria and categoria_clase_preguntada:
                        print(f"🔍 ===== PROCESANDO SLOT CONFIRMAR_CATEGORIA =====")
                        print(f"🔍 confirmar_categoria: '{confirmar_categoria}'")
                        
                        confirmar_lower = confirmar_categoria.lower().strip()
                        if any(p in confirmar_lower for p in ["por categoria", "por categoría", "categoria", "categoría", "si", "sí"]):
                            print(f"✅ Usuario eligió VER POR CATEGORÍAS: '{confirmar_categoria}'")

                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "categoria_clase"},
                                    "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": (
                                        "¡Perfecto! 🌟 Te ayudo a encontrar clases por categoría.\n\n"
                                        "🎯 **Selecciona tu categoría favorita:**\n\n"
                                        
                                        "💃 **Cardio y Ritmos** 🎵\n"
                                        "   ▸ Clases dinámicas para quemar calorías y divertirte\n"
                                        "   ▸ Ejemplos: Zumba, Aeróbicos, Baile\n\n"
                                        
                                        "🧘 **Cuerpo y Mente** ☯️\n"
                                        "   ▸ Clases para relajarte y conectar contigo mismo\n"
                                        "   ▸ Ejemplos: Yoga, Pilates, Meditación\n\n"
                                        
                                        "💪 **Fuerza y Tono** 🏋️‍♀️\n"
                                        "   ▸ Clases para fortalecer y tonificar tu cuerpo\n"
                                        "   ▸ Ejemplos: Functional, TRX, Body Pump\n\n"
                                        
                                        "🚴 **Indoor Cycling** ⚡\n"
                                        "   ▸ Clases intensas de ciclismo indoor\n"
                                        "   ▸ Ejemplos: Spinning, RPM, Cycle\n\n"
                                        
                                        "📚 **Otros** ✨\n"
                                        "   ▸ Clases especiales y actividades únicas\n"
                                        "   ▸ Ejemplos: Aqua aeróbicos, Boxeo, Crossfit\n\n"
                                        
                                        "💬 **Escribe el nombre de la categoría que más te guste:**"
                                    )
                                }]
                            }

                        elif any(p in confirmar_categoria.lower() for p in ["no", "ver todas", "todas", "todas las clases", "mostrar todas"]):
                            print(f"✅ Usuario eligió VER TODAS: '{confirmar_categoria}'")
                            # Mostrar todas las clases
                            clases = consultar_clases_por_sede_id(id_sede)
                            clases_nombres = [c['clase'] for c in clases] if clases and isinstance(clases[0], dict) else clases

                            if not clases_nombres:
                                return responder(f"No se encontraron clases para la sede {sede_raw}.", 
                                               session_attributes, intent_name, fulfillment_state="Fulfilled")

                            sede_mostrar = session_attributes.get("sede_nombre", sede_raw.title())

                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "clase"},
                                    "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": f"📋 **Todas las clases disponibles en {sede_mostrar}:**\n\n"
                                        + "\n".join(f"- {clase}" for clase in clases_nombres)
                                        if clases_nombres else f"No se encontraron clases en {sede_mostrar}."
                                        + "\n\n💬 ¿Cuál clase deseas consultar?"
                                }],
                                "responseCard": {
                                    "title": "Todas las clases disponibles",
                                    "buttons": [{"text": c, "value": c} for c in clases_nombres[:25]]
                                }
                            }
                        else:
                            # Respuesta no válida - volver a preguntar
                            print(f"❌ Respuesta no válida: '{confirmar_categoria}' - volviendo a preguntar")
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                                    "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": (
                                        "🤔 No entendí tu respuesta. ¿Cómo prefieres ver las clases?\n\n"
                                        
                                        "✅ **Respuestas válidas:**\n"
                                        "🔸 'Categoría' o 'Por categoría' → Para ver organizadas por tipo\n"
                                        "🔸 'Ver todas' o 'Todas' → Para ver lista completa\n\n"
                                        
                                        "💬 **Escribe tu preferencia:**"
                                    )
                                }]
                            }

                # 5. VALIDAR CLASE
                if clase_raw:
                    # AGREGAR: Calcular id_sede ANTES de validar clase
                    id_sede = obtener_id_sede(sede_raw)
                    if not id_sede:
                        return responder("No se pudo identificar la sede para validar las clases.", 
                                       session_attributes, intent_name, fulfillment_state="Fulfilled")
                    
                    print(f"🔍 Llamando validar_clase_usuario con clase_raw: '{clase_raw}' y id_sede: {id_sede}")
                    clase_id, clase_nombre, session_attributes, respuesta_clase = validar_clase_usuario(
                        slots, session_attributes, input_transcript, intent, id_sede
                    )
                    if respuesta_clase:
                        return respuesta_clase
                    if clase_nombre:
                        clase_raw = clase_nombre.lower()
                        print(f"🔍 Clase corregida a: '{clase_raw}'")
                        # Usar el nombre completo en el mensaje final
                        session_attributes["clase_display"] = clase_nombre
                        slots["clase"] = intent["slots"]["clase"]
                        print(f"✅ Slots actualizados después de corrección de clase")
                else:
                    # 4. ELICITAR CLASE si sede está presente pero clase no (código existente)
                    id_sede = obtener_id_sede(sede_raw)
                    clases = consultar_clases_por_sede_id(id_sede)
                    clases_nombres = [c['clase'] for c in clases] if clases and isinstance(clases[0], dict) else clases
                    
                    if not clases_nombres:
                        return responder(f"No se encontraron clases para la sede {sede_raw}.", 
                                       session_attributes, intent_name, fulfillment_state="Fulfilled")
                    
                    sede_mostrar = session_attributes.get("sede_nombre", sede_raw.title())
                    
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "clase"},
                            "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"📋 **Clases disponibles en {sede_mostrar}:**\n\n"
                                + "\n".join(f"- {clase}" for clase in clases_nombres)
                                if clases_nombres else f"No se encontraron clases en {sede_mostrar}."
                                + "\n\n💬 ¿Cuál deseas consultar?"
                        }],
                        "responseCard": {
                            "title": "Clases disponibles",
                            "buttons": [{"text": c, "value": c} for c in clases_nombres]
                        }
                    }

                                # 6. ELICITAR FECHA si no está presente
                if not fecha:
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
                            "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": "¿Para qué fecha deseas consultar los horarios de clase? Puedes escribir:\n• YYYY-MM-DD (2025-07-04)\n• DD de MMMM (25 de enero)\n• DD/MM (25/01)\n• 'hoy' o 'mañana'"
                        }]
                    }

                # 7. VALIDAR Y NORMALIZAR FECHA
                if fecha:
                    print(f"🔍 ===== DEBUG VALIDAR FECHA =====")
                    print(f"🔍 Fecha recibida del slot: '{fecha}'")
                    print(f"🔍 Tipo de fecha: {type(fecha)}")
                    fecha_normalizada, error_fecha = normalizar_fecha(fecha)
                    print(f"🔍 Resultado normalizar_fecha:")
                    print(f"🔍   - fecha_normalizada: '{fecha_normalizada}'")
                    print(f"🔍   - error_fecha: '{error_fecha}'")
                    print(f"🔍 =====================================")
                    
                    if error_fecha:
                        print(f" Error detectado en normalización: {error_fecha}")
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
                                "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": error_fecha
                            }]
                        }
                    # Usar la fecha normalizada
                    fecha = fecha_normalizada
                    print(f"✅ Fecha normalizada EXITOSA para consulta: '{fecha}'")
                else:
                    print("🔍 No hay fecha en el slot, saltando validación...")

                # 8. CONSULTAR HORARIOS (todos los slots están presentes)
                if not all([ciudad_raw, sede_raw, clase_raw, fecha]):
                    return responder("Faltan datos para consultar las clases grupales. Por favor, asegúrate de indicar ciudad, sede, clase y fecha.",
                                   session_attributes, intent_name, fulfillment_state="Fulfilled")

                id_sede = obtener_id_sede(sede_raw)
                id_clase = obtener_id_actividad(clase_raw)
                
                if not id_sede or not id_clase:
                    return responder("No se encontró la sede o clase indicada. Por favor, revisa los nombres.",
                                   session_attributes, intent_name, fulfillment_state="Fulfilled")

                horarios = consultar_horarios_por_sede_clase_fecha(id_sede, id_clase, fecha)
                if not horarios:
                    # No hay horarios disponibles - preguntar si desea otra consulta
                    mensaje_sin_horarios = f"No hay horarios disponibles para {clase_raw} en la sede {sede_raw} el {fecha}."
                    
                    session_attributes.pop("categoria_clase_preguntada", None)  
                    session_attributes.pop("clase_display", None)
                    session_attributes.pop("slots_previos", None)
                    
                    session_attributes["esperando_transicion_grupales"] = "true"
                    session_attributes["en_flujo_activo"] = "ConsultaGrupales"
                    
                    contenido = (
                        f"{mensaje_sin_horarios}\n\n"
                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                        "Selecciona una opción:\n"
                        "1️⃣ Otra ciudad\n"
                        "2️⃣ Otra sede\n"
                        "3️⃣ Otra clase\n"
                        "4️⃣ Otra categoría\n"
                        "5️⃣ No gracias\n\n"
                        "Responde con el número de tu opción (1, 2, 3, 4 o 5) o escribe directamente lo que deseas:"
                    )
                    
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                            "intent": {
                                "name": "ConsultaGrupales", 
                                "state": "InProgress",
                                "slots": {}
                            },
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": contenido
                        }]
                    }

                # Generar respuesta final
                horarios_str = "\n".join(f"- {h['hora_inicio']} a {h['hora_fin']}" for h in horarios)
                
                # Usar nombres completos guardados en sesión
                sede_display = session_attributes.get("sede_nombre", sede_raw.title())
                clase_display = session_attributes.get("clase_display", clase_raw.title())
                
                resumen_horarios = obtener_resumen_grupales(sede_display, clase_display, fecha, horarios)
                
                mensaje_final = respuesta_bedrock("ConsultaGrupales", resumen_horarios)
                if not mensaje_final or not mensaje_final.strip():
                    mensaje_final = (f"Horarios para {clase_display} en la sede {sede_display} el {fecha}:\n{horarios_str}")

                
                #guardar que se completó ConsultaGrupales
                session_attributes["ultimo_intent_completado"] = "ConsultaGrupales"
                
                # NUEVA LÓGICA: Preguntar directamente por nueva consulta
                session_attributes.pop("categoria_clase_preguntada", None)  
                session_attributes.pop("clase_display", None)
                session_attributes.pop("slots_previos", None)
                
                session_attributes["esperando_transicion_grupales"] = "true"
                session_attributes["en_flujo_activo"] = "ConsultaGrupales"
                
                contenido = (
                    f"{mensaje_final}\n\n"
                    "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                    "Selecciona una opción:\n"
                    "1️⃣ Otra ciudad\n"
                    "2️⃣ Otra sede\n"
                    "3️⃣ Otra clase\n"
                    "4️⃣ Otra categoría\n"
                    "5️⃣ No gracias\n\n"
                    "Responde con el número de tu opción (1, 2, 3, 4 o 5) o escribe directamente lo que deseas:"
                )
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                        "intent": {
                            "name": "ConsultaGrupales", 
                            "state": "InProgress",
                            "slots": {}
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": contenido
                    }]
                }

            except Exception as e:
                print("❌ Error en ConsultaGrupales:", str(e))
                return responder("Lo siento, ha ocurrido un error al consultar las actividades. Intenta nuevamente más tarde.",
                               session_attributes, intent_name)        
        # -----------------------------
        # FLUJO: ConsultarSedes
        # -----------------------------
        if intent_name == "ConsultarSedes":
            session_attributes["en_flujo_activo"] = intent_name
            linea = (get_slot_value(slots, "linea") or "").strip().lower()
            
            if linea in ["body", "bodytech"]:
                linea = "bodytech"
            elif linea in ["athletic", "athletic club"]:
                linea = "athletic"
            else: 
                linea = ""
                
            ciudad = session_attributes.get("ciudad") or get_slot_value(slots, "ciudad")
            categoria = get_slot_value(slots, "categoria")
            pregunta_categoria = session_attributes.get("pregunta_categoria")

            # 1. Preguntar por línea si no está
            if not linea:
                session_attributes.pop("linea", None)
                session_attributes["pregunta_categoria"] = None
                
                contenido = (
                    "¿Deseas consultar las sedes de la línea Bodytech o Athletic?\n\n"
                    "Responde: 'Bodytech' o 'Athletic'"
                )
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "linea"},
                        "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{"contentType": "PlainText", "content": contenido}]
                }
            # Solo guardar la línea después de la confirmación del usuario
            session_attributes["linea"] = linea

            # 2. Preguntar por ciudad si no está
            if not ciudad:
                session_attributes["pregunta_categoria"] = None
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                        "intent": {
                            "name": intent_name,
                            "slots": slots,
                            "state": "InProgress"
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿En qué ciudad deseas consultar las sedes?"
                    }]
                }
            session_attributes["ciudad"] = ciudad

            # 3. Validar ciudad y obtener sedes
            print(f"DEBUG slots: {slots}")
            print(f"DEBUG input_transcript: {input_transcript}")
            ciudad_id, ciudad_nombre, session_attributes, respuesta_ciudad = validar_ciudad_usuario(
                slots, session_attributes, input_transcript, intent
            )
            print(f"DEBUG ciudad_id: {ciudad_id}, ciudad_nombre: {ciudad_nombre}")
            if not ciudad_id:
                return respuesta_ciudad
            
            if linea == "athletic":
            # Athletic: solo muestra sedes, no pregunta por categoría
               brand_id = 2
               sedes = consultar_sedes_por_ciudad_id(ciudad_id, brand_id)
               sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
               mensaje = (
                   f"Sedes Athletic en {ciudad_nombre.title()}:\n"
                   + "\n".join(f"- {s}" for s in sedes_nombres)
                   if sedes_nombres else "No se encontraron sedes Athletic para la ciudad seleccionada."
               )
               session_attributes["esperando_respuesta_final"] = "true"
               return responder_con_pregunta_final(mensaje, session_attributes, intent_name)
            else:
    # 4. Mostrar sedes y preguntar si desea filtrar por categoría SOLO para bodytech
               if pregunta_categoria is None and linea == "bodytech":
                    brand_id = 1
                    sedes = consultar_sedes_por_ciudad_id(ciudad_id, brand_id)
                    sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                    if not sedes_nombres:
                        return responder(
                            f"No se encontraron sedes para la ciudad {ciudad_nombre}.",
                            session_attributes, intent_name, fulfillment_state="Fulfilled"
                        )
                    session_attributes["pregunta_categoria"] = "pendiente"

            # 5. Procesar respuesta a la pregunta de categoría
            if pregunta_categoria == "pendiente":
                # Lee el valor del slot opcional
                confirmar = get_slot_value(slots, "confirmar_mostrar_sedes")
                if confirmar:
                    if any(p in confirmar.lower() for p in ["sí", "si", "yes"]):
                        session_attributes["pregunta_categoria"] = "si"
                    elif any(p in confirmar.lower() for p in ["no"]):
                        session_attributes["pregunta_categoria"] = "no"
                    else:
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                                "intent": {
                                    "name": intent_name,
                                    "slots": slots,
                                    "state": "InProgress"
                                },
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": "¿Quieres consultar las sedes por categoría?\n\nResponde:\n• 'Sí' para ver por categoría\n• 'No' para ver todas las sedes"
                        }]}
        
                else:
                    # Si no hay valor, vuelve a elicitar el slot
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                            "intent": {
                                "name": intent_name,
                                "slots": slots,
                                "state": "InProgress"
                            },
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": "¿Quieres consultar las sedes por categoría? (responde sí o no)"
                        }]
                    }

            # 6. Si quiere por categoría, elicita el slot categoria y filtra
            if session_attributes.get("pregunta_categoria") == "si":
                categorias = obtener_categorias_por_linea(linea)
                categorias_normalizadas = [normalizar_nombre(c) for c in categorias]
                categoria_usuario = normalizar_nombre(categoria)
                print("DEBUG categorias:", categorias)
                print("DEBUG categorias_normalizadas:", categorias_normalizadas)
                print("DEBUG categoria_usuario:", categoria_usuario)
                print("DEBUG categoria slot:", categoria)
                brand_id = 1 if linea.strip().lower() == "bodytech" else 2

                if not categoria or categoria_usuario not in categorias_normalizadas:
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "categoria"},
                            "intent": {
                                "name": intent_name,
                                "slots": slots,
                                "state": "InProgress"
                            },
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"Estas son las categorías disponibles para la línea {linea.title()}: {', '.join(categorias)}. ¿Cuál deseas consultar?"
                        }]
                    }
                # Consulta sedes por ciudad y categoría
                categoria_valida = categorias[categorias_normalizadas.index(categoria_usuario)]
                id_categoria = obtener_id_categoria_por_nombre(categoria_valida, brand_id)
                print(f"DEBUG: Consultando sedes para brand_id={brand_id}, id_categoria={id_categoria}, ciudad_id={ciudad_id}")
                if not id_categoria:
                    return responder(
                        "No se encontró la categoría seleccionada para la línea indicada.",
                        session_attributes,
                        intent_name,
                        fulfillment_state="Fulfilled"
                    )
                sedes = consultar_sedes_por_ciudad_id_linea(brand_id, id_categoria, ciudad_id)
                print(f"DEBUG: Resultado sedes={sedes}")
                sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                mensaje = (
                    f"Sedes en {ciudad_nombre.title()} para la categoría {categoria_valida.title()}:\n"
                    + "\n".join(f"- {s}" for s in sedes_nombres)
                    if sedes_nombres else "No se encontraron sedes para esa categoría en la ciudad seleccionada."
                )
            else:
                # Solo consulta sedes por ciudad
                brand_id = 1 if linea.strip().lower() == "bodytech" else 2
                sedes = consultar_sedes_por_ciudad_id(ciudad_id, brand_id)
                sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                mensaje = (
                    f"Sedes en {ciudad_nombre.title()}:\n"
                    + "\n".join(f"- {s}" for s in sedes_nombres)
                    if sedes_nombres else "No se encontraron sedes para la ciudad seleccionada."
                )

            session_attributes.pop("en_flujo_activo", None)
            session_attributes["esperando_respuesta_final"] = "true"
            return responder_con_pregunta_final(mensaje, session_attributes, intent_name)
        # -----------------------------
        # FLUJO: SALUDO + HÁBEAS DATA
        # -----------------------------
        if intent_name == "SaludoHabeasData":
            session_attributes["en_flujo_activo"] = intent_name
            saludos_validos = ["hola", "buenas", "saludos", "hey", "qué tal", "buenos días", "buenas tardes"]

            if session_attributes.get("acepto_politicas") == "true":
                if any(s in input_transcript for s in saludos_validos):
                    return responder("¡Hola nuevamente! ¿En qué más puedo ayudarte?", session_attributes, intent_name)
                else:
                    print("⚠️ Frase clasificada como saludo pero no parece un saludo real. Mostrando sugerencias.")
                    return mostrar_sugerencias(session_attributes)

            if session_attributes.get("politicas_mostradas") == "true":
                if any(p in input_transcript for p in ["si", "sí", "acepto", "de acuerdo", "vale", "claro", "ok", "bueno", "listo", "está bien","por supuesto"]):
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
                            "¿En qué puedo ayudarte?\n\n"
                            "Algunas opciones:\n"
                            "📄 Preguntas frecuentes sobre Bodytech\n"
                            "🏢 Consultar sedes y horarios\n"
                            "🏃‍♂️ Clases grupales disponibles\n"
                            "📅 Información de tu plan\n"
                            "👥 Consultar invitados\n"
                            "🏆 Información sobre referidos\n"
                            "🧾 Consultar incapacidades\n"
                            "🛍️ Información de ventas\n\n"
                            "¿Sobre qué tema te gustaría que te ayude?"
                            )
                        }]
                    }

                if any(p in input_transcript for p in [
                    "no", "rechazo", "no acepto", "no deseo continuar", 
                    "no quiero continuar", "no deseo", "no quiero",
                    "decline", "rechazar", "olvidalo", "claro que no", 
                    "por supuesto que no", "despedida", "adiós", "bye",
                    "no quiero nada", "cancelar", "salir"
                ]):
                    return terminar_sin_calificacion(
                        "Gracias por contactarte con nosotros. Lamentablemente no podemos continuar si no aceptas nuestras políticas de tratamiento de datos.",
                        session_attributes
                    )

                return responder("¿Deseas continuar y aceptar nuestras políticas de tratamiento de información?", session_attributes, intent_name)

            # Primer contacto con esta intención
            session_attributes["politicas_mostradas"] = "true"
            mensaje = (
                "Bienvenid@ al Servicio al Cliente de Bodytech soy Milo tu asistente virtual! "
                "Al continuar con esta comunicación estás de acuerdo con nuestra política de manejo de datos: "
                "https://bodytech.com.co/tratamiento-de-informacion\n\n¿Deseas continuar?"
            )
            return responder(mensaje, session_attributes, intent_name)
                # -----------------------------
        # FLUJO: Despedida
        # -----------------------------
        if intent_name == "Despedida":
            return cerrar_conversacion(
            "¡Gracias por contactarte con nosotros! Que tengas un excelente día. 😊",  # ✅ MENSAJE PRIMERO
            session_attributes  # ✅ SESSION_ATTRIBUTES SEGUNDO
        )
            
        # -----------------------------
        # FLUJO: ConsultaInfoPlan
        # -----------------------------

        if intent_name == "ConsultaInfoPlan":
            try:
                session_attributes["en_flujo_activo"] = intent_name
                # 1. Validar tipo y número de documento (centralizado)
                document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                    slots, session_attributes, input_transcript, intent
                )
                print("📋 document_type_id:", document_type_id)
                print("📋 document_number:", document_number)
                if respuesta_incompleta:
                    return respuesta_incompleta
                
                if document_type_id is None or document_number is None:
                    # Esto es redundante, pero por seguridad
                    return responder("Faltan datos para continuar.", session_attributes, intent_name)        
                # Guarda los datos del documento para futuras intenciones
                session_attributes["document_type_id"] = str(document_type_id)
                session_attributes["document_number"] = str(document_number)
                print(f"✅ Tipo documento mapeado: {document_type_id}, Número: {document_number}")
                print("🕐 Enviando mensaje de espera al usuario...")
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

                return responder_con_pregunta_final(mensaje_final, session_attributes, intent_name)

            except Exception as e:
                print("❌ Error en ConsultaInfoPlan:", str(e))
                traceback.print_exc()
                return responder("Lo siento, ha ocurrido un error al procesar tu solicitud. Intenta nuevamente más tarde.", session_attributes, intent_name)




        # -----------------------------
        # 4️⃣ FLUJO: FQABodytech
        # -----------------------------
        if intent_name == "FQABodytech":
            try:
                session_attributes["en_flujo_activo"] = intent_name
                config = obtener_secret("main/LexAgenteVirtualSAC")
                prompt = get_prompt_por_intent(intent_name, input_transcript)
                respuesta_kb = consultar_kb_bedrock(prompt, config["BEDROCK_KB_ID_FQABodytech"])
                mensaje_final = f"{respuesta_kb.strip()}\n\n¿Puedo ayudarte con algo más? 🤗"
                session_attributes.pop("en_flujo_activo", None)
                session_attributes["esperando_respuesta_final"] = "true"

                return responder_con_pregunta_final(mensaje_final, session_attributes, intent_name)
            except Exception as e:
                print("❌ Error en FQABodytech:", str(e))
                return responder("Lo siento, hubo un problema consultando la información. Intenta más tarde.", session_attributes, intent_name)

        # -----------------------------
        # FLUJO: Venta
        # -----------------------------
        if intent_name == "Venta":
            try:
                session_attributes["en_flujo_activo"] = intent_name
                config = obtener_secret("main/LexAgenteVirtualSAC")
                prompt = get_prompt_por_intent(intent_name, input_transcript)
                kb_id = config.get("BEDROCK_KB_ID_Venta")
                print(f"🔍 KB ID obtenido: {kb_id}")
                if kb_id:
                    print("🔍 Procesando con KB...")
                    respuesta_kb = consultar_kb_bedrock(prompt, kb_id)
                    mensaje_final = respuesta_kb.strip()
                    print(f"🔍 Respuesta KB: '{mensaje_final[:100]}...'")
                else:
                    print("🔍 Procesando sin KB (mensaje estático)...")
                    campaign_id = config.get("campain_ventas", "1")
                    mensaje_final = f"🛍️ ¡Gracias por tu interés!\nUn asesor de nuestro equipo estará contigo en breve para ayudarte con tu compra 😊"
                    print(f"🔍 Campaign ID: {campaign_id}")
                session_attributes.pop("en_flujo_activo", None)
                session_attributes["esperando_respuesta_final"] = "true"
                print(f"🔍 esperando_respuesta_final marcado: {session_attributes.get('esperando_respuesta_final')}")
                print("🔍 ===== DEBUG VENTA - FIN =====")
                
                return responder_con_pregunta_final(mensaje_final, session_attributes, intent_name)

            except Exception as e:
                print("❌ Error en intención Venta:", str(e))
                mensaje = "Lo siento, hubo un problema procesando tu solicitud. Intenta más tarde."
                return responder_con_pregunta_final(mensaje, session_attributes, intent_name)

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
                session_attributes["en_flujo_activo"] = intent_name
                # 1. Revisar si ya hay info del plan en sesión
                datos_plan_json = session_attributes.get("datos_plan_json")
                if datos_plan_json:
                    print("♻️ Usando información de plan existente en sesión")
                    datos_plan = json.loads(datos_plan_json)
                    error_msg = None
                else:
                    # 2. Validar tipo y número de documento (centralizado)
                    document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                        slots, session_attributes, input_transcript, intent
                    )
                    print("📋 document_type_id:", document_type_id)
                    print("📋 document_number:", document_number)
                    if respuesta_incompleta:
                        return respuesta_incompleta
                    
                    if document_type_id is None or document_number is None:
                        # Esto es redundante, pero por seguridad
                        return responder("Faltan datos para continuar.", session_attributes, intent_name)

                    # Guarda los datos del documento para futuras intenciones
                    session_attributes["document_type_id"] = str(document_type_id)
                    session_attributes["document_number"] = str(document_number)
                    print(f"✅ Tipo documento mapeado: {document_type_id}, Número: {document_number}")
                    print("🕐 Enviando mensaje de espera al usuario...")
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
                session_attributes.pop("esperando_respuesta_final", None)
                session_attributes["esperando_respuesta_final"] = "true"
                return responder_con_pregunta_final(mensaje, session_attributes, intent_name)

            except Exception as e:
                print("❌ Error en CongelarPlan:", str(e))
                return responder("Lo siento, hubo un error al validar la congelación de tu plan. Intenta más tarde.", session_attributes, intent_name)            
        # -----------------------------
        # FLUJO: FQAReferidos
        # -----------------------------
        if intent_name == "FQAReferidos":
            session_attributes["en_flujo_activo"] = intent_name
            try: 
                document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                    slots, session_attributes, input_transcript, intent
                )
                if respuesta_incompleta:
                    return respuesta_incompleta
                if document_type_id is None or document_number is None:
                    return responder("Faltan datos para continuar.", session_attributes, intent_name)
                session_attributes["document_type_id"] = str(document_type_id)
                session_attributes["document_number"] = str(document_number)

                datos_referidos, error_msg = consultar_referidos(document_type_id, document_number)
                if error_msg and "error" in error_msg.lower():
                    return responder_con_pregunta_final(error_msg, session_attributes, intent_name)

                referidos_activos = datos_referidos and datos_referidos.get("data")
                if referidos_activos:
                    # Caso: SÍ hay referidos - usar prompt normal
                    mensaje_final = respuesta_bedrock("FQAReferidos", datos_referidos)
                    session_attributes["esperando_info_referidos"] = "true"
                    return crear_respuesta_info_adicional(
                        mensaje_final,
                        session_attributes,
                        intent_name,
                        "esperando_info_referidos")
                else:
                    # Si no hay referidos consulta la KB y responde con esa información
                    prompt = get_prompt_no_info("FQAReferidos", "")
                    mensaje_final = consultar_bedrock_generacion(prompt)
                    session_attributes["esperando_info_referidos"] = "true"
                    return crear_respuesta_info_adicional(
                        mensaje_final,
                        session_attributes,
                        intent_name,
                        "esperando_info_referidos") 
            except Exception as e:
                print("❌ Error en ConsultarReferidos:", str(e))
                return responder("Lo siento, ha ocurrido un error al procesar tu solicitud. Intenta nuevamente más tarde.", session_attributes, intent_name)

        # -----------------------------
        # FLUJO: Fallback personalizado
        # -----------------------------
        if intent_name == "FallbackIntent":
            return mostrar_sugerencias(session_attributes)
        
        # -----------------------------
        # FLUJO: Ingresos de Compañia
        # -----------------------------
        if intent_name == "Ingresos":
            session_attributes["en_flujo_activo"] = intent_name
            print("DEBUG slots recibidos:", slots)
            linea = get_slot_value(slots, "linea")
            tipo = get_slot_value(slots, "tipo_consulta")
            confirmar_segmento = get_slot_value(slots, "confirmar_mostrar_sedes")
            segmento = get_slot_value(slots, "segmento")
            Fecha = get_slot_value(slots, "Fecha")
            fecha_fin = get_slot_value(slots, "fecha_fin")
            sede_nombre = get_slot_value(slots, "sede")
            sede_nombre = sede_nombre.split(",")[0].strip().lower() if sede_nombre else None
            sede_nombre_normalizado = normalizar_nombre(sede_nombre) if sede_nombre else None
            sede_id = obtener_id_sede(sede_nombre_normalizado) if sede_nombre_normalizado else None
            
            segmento_map = { "Administrativo": 90, "Corporativo": 5, "Masivo": 4}
            segmento_id = segmento_map.get(segmento) if segmento else None
            
            
            print("DEBUG linea:", linea)
            print("DEBUG tipo:", tipo)
            print("DEBUG fecha:", Fecha)
            print("DEBUG sede_nombre:", sede_nombre)
            print("DEBUG sede_id:", sede_id)
            
            
            if not linea:
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "linea"},
                        "intent": {"name": intent_name, "state": "InProgress"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿Sobre qué línea de negocio deseas consultar los ingresos? (Bodytech o Athletic)"
                    }]
        }

            if not tipo:
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_consulta"},
                        "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿Quieres saber el total de la compañía o solo por sede?"
                    }]
        }

            if tipo == "Por sede" and not sede_nombre:
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
                        "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿Para qué sede deseas consultar los ingresos?"
                    }]
                }
            if not confirmar_segmento:
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                        "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿Deseas consultar los ingresos por segmento?"
                }]
                }
            if confirmar_segmento.lower() in ["sí", "si"]:
                if not segmento:
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "segmento"},
                            "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": "¿Por cuál segmento deseas consultar los ingresos?"
                        }]
                    }
            # Validar y normalizar fechas
            if not Fecha:
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "Fecha"},
                        "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿Cuál es la **fecha inicial** para la consulta? Puedes escribir:\n• 2025-05-01\n• 1 de mayo de 2025\n• 1 de mayo\n• 01/05/2025\n• 'hoy' o 'mañana'"
                    }]
                }
            
            if not fecha_fin:
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha_fin"},
                        "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿Cuál es la **fecha final** para la consulta? Puedes escribir:\n• 2025-05-31\n• 31 de mayo de 2025\n• 31 de mayo\n• 31/05/2025\n• 'hoy' o 'mañana'"
                    }]
                }

            # SEGUNDO: Validar y normalizar fechas (solo si ambas están presentes)
            if Fecha:
                fecha_normalizada, error_fecha = normalizar_fecha(Fecha)
                if error_fecha:
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "Fecha"},
                            "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"Error en fecha inicial: {error_fecha}"
                        }]
                    }
                Fecha = fecha_normalizada
            
            if fecha_fin:
                fecha_fin_normalizada, error_fecha_fin = normalizar_fecha(fecha_fin)
                if error_fecha_fin:
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha_fin"},
                            "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"Error en fecha final: {error_fecha_fin}"
                        }]
                    }
                fecha_fin = fecha_fin_normalizada
                
            
            sede_id = obtener_id_sede(sede_nombre) if sede_nombre else None
            
            query = armar_consulta_ingresos(linea, tipo, Fecha, fecha_fin, sede_id, segmento_id)
            resultado = ejecutar_consulta(query)

            # Armar resumen para Bedrock
            resumen = {
                "linea": linea,
                "tipo": tipo,
                "fecha_inicio": Fecha,
                "fecha_fin": fecha_fin,
                "sede": sede_nombre if sede_nombre else "toda la compañía",
                "segmento": segmento if segmento else "Todos",
                "ingresos": resultado
            }

            # Llamar a Bedrock para generar la respuesta natural
            mensaje_final = respuesta_bedrock("Ingresos", resumen)
            session_attributes.pop("en_flujo_activo", None)
            session_attributes["esperando_respuesta_final"] = "true"
            
            return responder_con_pregunta_final(mensaje_final, session_attributes, intent_name)
        # -----------------------------
        # FLUJO: ConsultarInvitados
        # -----------------------------
        
        if intent_name == "ConsultarInvitados":
            try:
                session_attributes["en_flujo_activo"] = intent_name
                # 1. Validar tipo y número de documento (centralizado)
                document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                    slots, session_attributes, input_transcript, intent
                )
                print("📋 document_type_id:", document_type_id)
                print("📋 document_number:", document_number)
                if respuesta_incompleta:
                    return respuesta_incompleta
                
                if document_type_id is None or document_number is None:
                    # Esto es redundante, pero por seguridad
                    return responder("Faltan datos para continuar.", session_attributes, intent_name)        
                # Guarda los datos del documento para futuras intenciones
                session_attributes["document_type_id"] = str(document_type_id)
                session_attributes["document_number"] = str(document_number)
                print(f"✅ Tipo documento mapeado: {document_type_id}, Número: {document_number}")
                print("🕐 Enviando mensaje de espera al usuario...")
                print("🟡 Esperando mientras se realiza consulta API")
                
                # 2. Consultar invitados
                datos_invitados, error_msg = consultar_invitados(document_type_id, document_number)
                if error_msg and "error" in error_msg.lower():
                    return responder_con_pregunta_final(error_msg, session_attributes, intent_name)
                
                invitados_activos = datos_invitados and datos_invitados.get("data")
                if invitados_activos:
                    # Caso: SÍ hay invitados - usar prompt normal
                    mensaje_final = respuesta_bedrock("ConsultarInvitados", datos_invitados)
                    session_attributes["esperando_info_invitados"] = "true"
                    return crear_respuesta_info_adicional(
                        mensaje_final, 
                        session_attributes, 
                        intent_name, 
                        "esperando_info_invitados"
                    )
                else:
                    # Si no hay invitados consulta la KB y responde con esa información
                    prompt = get_prompt_no_info("ConsultarInvitados", "")
                    mensaje_final = consultar_bedrock_generacion(prompt)
                    session_attributes["esperando_info_invitados"] = "true"
                    return crear_respuesta_info_adicional(
                        mensaje_final, 
                        session_attributes, 
                        intent_name, 
                        "esperando_info_invitados"
                    )
            except Exception as e:
                print("❌ Error en CongelarPlan:", str(e))
                return responder("Lo siento, hubo un error al validar los invitados de tu plan. Intenta más tarde.", session_attributes, intent_name) 
        
        # -----------------------------
        # FLUJO: Consulta de Incapacidades
        # -----------------------------
        
        if intent_name == "ConsultaIncapacidades":
            try:
                session_attributes["en_flujo_activo"] = intent_name
                # 1. Validar tipo y número de documento (centralizado)
                document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                    slots, session_attributes, input_transcript, intent
                )
                print("📋 document_type_id:", document_type_id)
                print("📋 document_number:", document_number)
                if respuesta_incompleta:
                    return respuesta_incompleta
                 # Guarda los datos del documento para futuras intenciones
                session_attributes["document_type_id"] = str(document_type_id)
                session_attributes["document_number"] = str(document_number)
                print(f"✅ Tipo documento mapeado: {document_type_id}, Número: {document_number}")
                print("🕐 Enviando mensaje de espera al usuario...")
                print("🟡 Esperando mientras se realiza consulta API")
                if document_type_id is None or document_number is None:
                    # Esto es redundante, pero por seguridad
                    return responder("Faltan datos para continuar.", session_attributes, intent_name)        
               
                datos_incapacidades, error_msg = consultar_incapacidades(document_type_id, document_number)
                if error_msg and "error" in error_msg.lower():
                    return responder_con_pregunta_final(error_msg, session_attributes, intent_name)
                
                incapacidades_activas = datos_incapacidades and datos_incapacidades.get("data")
                if incapacidades_activas:
                    
                    mensaje_final = respuesta_bedrock("ConsultaIncapacidades", datos_incapacidades)
                    session_attributes["esperando_info_incapacidad"] = "true"
                    return crear_respuesta_info_adicional(
                        mensaje_final, 
                        session_attributes, 
                        intent_name, 
                        "esperando_info_incapacidad"
                    )
                else:
                    prompt = get_prompt_no_info("ConsultaIncapacidades", "")
                    mensaje_final = consultar_bedrock_generacion(prompt)
                    session_attributes["esperando_info_incapacidad"] = "true"
                    return crear_respuesta_info_adicional(
                        mensaje_final, 
                        session_attributes, 
                        intent_name, 
                        "esperando_info_incapacidad"
                    )
            except Exception as e:
                print("❌ Error en ConsultaIncapacidades:", str(e))
                return responder("Lo siento, ha ocurrido un error al procesar tu solicitud. Intenta nuevamente más tarde.", session_attributes, intent_name)
            
    except Exception as e:
        print("❌ Error general en Lambda:", str(e))
        return responder("Lo siento, ha ocurrido un error inesperado.", {}, "FallbackIntent")


        









