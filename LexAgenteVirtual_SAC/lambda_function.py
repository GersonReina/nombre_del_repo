import json
import traceback
import difflib
import unicodedata
import re
from utils import *
from prompts import get_prompt_por_intent, get_prompt_no_info, get_prompt_info
from respuestas import respuesta_bedrock, obtener_respuesta_congelacion, consultar_bedrock_generacion
from services import validar_documento_usuario, consultar_kb_bedrock, manejar_respuesta_post_pregunta_adicional, normalizar_nombre, validar_sede_usuario, validar_clase_usuario, validar_y_forzar_flujo_ciudad, extraer_y_validar_slots_grupales
from services import validar_ciudad_usuario, obtener_id_sede, obtener_id_actividad, get_slot_value, obtener_categorias_por_linea, obtener_id_categoria_por_nombre, obtener_nombre_actividad_por_id, flujo_grupales_por_ciudad, extraer_y_validar_slots_sedes
from redshift_utils import consultar_sedes_por_ciudad_id, consultar_clases_por_sede_id, consultar_horarios_por_sede_clase_fecha,consultar_sedes_por_ciudad_id_linea, armar_consulta_ingresos, ejecutar_consulta, consultar_horarios_sede, consultar_clases_grupales_por_sede_fecha
from secret import obtener_secret

def lambda_handler(event, context):
    print("📥 Evento recibido:", json.dumps(event, indent=2))
    session_state = event.get("sessionState", {})
    session_attributes = session_state.get("sessionAttributes", {}) or {}
    input_transcript = event.get("inputTranscript", "").lower()
    
    # Solo procesar "M" si las políticas ya fueron aceptadas
    if (input_transcript.strip().lower() in ["m", "menu", "menú", "menu principal", "menú principal"] and
        session_attributes.get("acepto_politicas") == "true"):
        
        # PRESERVAR datos de documento antes de limpiar
        documento_attrs = {
            "document_type_id": session_attributes.get("document_type_id"),
            "document_type_raw": session_attributes.get("document_type_raw"),
            "document_number": session_attributes.get("document_number"),
            "intenciones_con_documento": session_attributes.get("intenciones_con_documento")
        }
        session_attributes.clear()
        # Restaurar datos de documento si existen
        for k, v in documento_attrs.items():
            if v is not None:
                session_attributes[k] = v
                
        session_attributes["acepto_politicas"] = "true"
        return mostrar_menu_principal(session_attributes)
    # Si la conversación ya fue finalizada, no responder más
    if session_attributes.get("conversacion_finalizada") == "true":
        # Limpiar todos los atributos de sesión para reiniciar
        session_attributes = {}
        # Forzar el flujo a la intención de bienvenida/políticas
        event["sessionState"]["intent"] = {"name": "SaludoHabeasData", "slots": {}}
        event["sessionState"]["sessionAttributes"] = session_attributes
    try:
        
        session_state = event.get("sessionState", {})
        intent = session_state.get("intent", {})
        intent_name = intent.get("name", "")
        session_attributes = session_state.get("sessionAttributes", {}) or {}
        
        input_transcript = event.get("inputTranscript", "").lower()
        slots = intent.get("slots", {})

        # ✅ DEBUG: Información del flujo de documento
        print(f"🔍 DEBUG FLUJO DOCUMENTO:")
        print(f"  - Intent detectado: {intent_name}")
        print(f"  - Input usuario: '{input_transcript}'")
        print(f"  - En flujo activo: {session_attributes.get('en_flujo_activo')}")
        print(f"  - Tiene document_type_id: {session_attributes.get('document_type_id')}")
        print(f"  - Tiene document_number: {session_attributes.get('document_number')}")
        print(f"  - Slots actuales: {slots}")

        # ✅ CRÍTICO: Detectar sesión nueva con intent incorrecto
        es_sesion_nueva = not session_attributes or len(session_attributes) == 0
        politicas_no_aceptadas = session_attributes.get("acepto_politicas") != "true"
        
        # Si es sesión nueva Y no han aceptado políticas Y el intent no es SaludoHabeasData
        if es_sesion_nueva and politicas_no_aceptadas and intent_name != "SaludoHabeasData":
            print(f"🔄 REDIRIGIENDO: Sesión nueva con intent '{intent_name}' → SaludoHabeasData")
            
            # Marcar que ya se mostraron las políticas para evitar duplicación
            session_attributes = {"politicas_mostradas": "true"}
            
            # Forzar redirección a SaludoHabeasData
            return {
                "sessionState": {
                    "dialogAction": {"type": "ElicitIntent"},
                    "intent": {
                        "name": "SaludoHabeasData",
                        "state": "InProgress",
                        "slots": {}
                    },
                    "sessionAttributes": session_attributes
                },
                "messages": [{
                    "contentType": "PlainText",
                    "content": (
                        "Bienvenid@ al Servicio al Cliente de Bodytech soy Milo tu asistente virtual! "
                        "Al continuar con esta comunicación estás de acuerdo con nuestra política de manejo de datos: "
                        "https://bodytech.com.co/tratamiento-de-informacion\n\n¿Deseas continuar?"
                    )
                }]
            }
        
        
        intenciones_con_documento = session_attributes.get("intenciones_con_documento", "")
        intenciones_set = set(intenciones_con_documento.split(",")) if intenciones_con_documento else set()
        flujo_activo = session_attributes.get("en_flujo_activo")
        hay_politicas_aceptadas = session_attributes.get("acepto_politicas") == "true"
        
        if flujo_activo and hay_politicas_aceptadas:
            respuesta_transicion_grupales = esperando_respuesta_grupales(session_attributes, input_transcript, slots, intent)
            if respuesta_transicion_grupales:
                 return respuesta_transicion_grupales

            respuesta_transicion_sedes = esperando_respuesta_sedes(session_attributes, input_transcript, slots, intent)
            if respuesta_transicion_sedes:
                return respuesta_transicion_sedes
        
        intenciones_protegidas = [
             "ConsultaGrupales", "ConsultarInvitados", "FQAReferidos","ConsultarSedes", "FQABodytech", "Venta", 
             "ConsultaIncapacidades", "ConsultaInfoPlan", "CongelarPlan", "Ingresos", "ConsultaHorarios"
              ]
        intenciones_que_interrumpen = [
            "FQABodytech", "Venta", "ConsultarSedes", "ConsultaGrupales",
            "ConsultarInvitados", "FQAReferidos", "ConsultaIncapacidades", 
            "ConsultaInfoPlan", "CongelarPlan", "Ingresos", "SaludoHabeasData", "ConsultaHorarios"
        ]
        
        # PRIORIDAD 0: Manejar respuesta sobre hablar con asesor
        if session_attributes.get("esperando_respuesta_asesor") == "true":
            from utils import procesar_respuesta_asesor
            respuesta_asesor = procesar_respuesta_asesor(input_transcript, session_attributes)
            if respuesta_asesor:
                return respuesta_asesor
        
        # PRIORIDAD 1: Manejar CALIFICACIÓN PRIMERO
        if session_attributes.get("esperando_calificacion") == "true":
            session_attributes.pop("esperando_calificacion", None)
            
            # 🆕 VERIFICAR SI DEBE IR A ASESOR DESPUÉS DE CALIFICAR
            ir_a_asesor_despues = session_attributes.get("despues_calificacion_asesor") == "true"
            if ir_a_asesor_despues:
                session_attributes.pop("despues_calificacion_asesor", None)
            
            # Extraer número del input
            calificacion_input = input_transcript.strip()
            
            # Verificar si es un número válido del 1 al 5
            try:
                calificacion = int(calificacion_input)
                
                if 1 <= calificacion <= 5:
                    # Crear mensaje con estrellas
                    estrellas = "⭐" * calificacion
                    
                    # 🆕 SI DEBE IR A ASESOR, MENSAJE DIFERENTE
                    if ir_a_asesor_despues:
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "Close"},
                                "intent": {"name": "TransferenciaAsesor", "state": "Fulfilled"},
                                "sessionAttributes": {"conversacion_finalizada": "true"}
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    f"¡Gracias por tu calificación! {estrellas}\n\n"
                                    "Te estamos transfiriendo con uno de nuestros asesores especializados.\n\n"
                                    "En un momento estarás conectado para recibir ayuda personalizada. 👨‍💼"
                                )
                            }]
                        }
                    
                    # Mensaje personalizado según la calificación (lógica normal)
                    if calificacion == 5:
                        mensaje_agradecimiento = f"¡Excelente! {estrellas}\n\n¡Nos alegra saber que tuviste una experiencia fantástica! 😊"
                    elif calificacion == 4:
                        mensaje_agradecimiento = f"¡Muy buena! {estrellas}\n\n¡Gracias por tu valoración positiva! 😊"
                    elif calificacion == 3:
                        mensaje_agradecimiento = f"Regular {estrellas}\n\n¡Gracias por tu calificación! Trabajaremos para mejorar. 😊"
                    elif calificacion == 2:
                        mensaje_agradecimiento = f"Mala {estrellas}\n\n¡Gracias por tu honestidad! Nos ayuda a mejorar nuestro servicio. 😊"
                    else:  # calificacion == 1
                        mensaje_agradecimiento = f"Muy mala {estrellas}\n\n¡Gracias por tu feedback! Tomaremos medidas para mejorar. 😊"
                    session_attributes["conversacion_finalizada"] = "true"
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
                            "content": f"{mensaje_agradecimiento}\n\n¡Que tengas un excelente día! 🌟"
                        }]
                    }
                else:
                    # Número fuera del rango 1-5
                    session_attributes["esperando_calificacion"] = "true"
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitIntent"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                f"🤔 '{calificacion_input}' no es una calificación válida.\n\n"
                                "Por favor, selecciona un número del 1 al 5:\n\n"
                                "⭐ 1 estrella - Muy mala\n"
                                "⭐⭐ 2 estrellas - Mala\n"
                                "⭐⭐⭐ 3 estrellas - Regular\n"
                                "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                                "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                                "💬 **Responde con un número del 1 al 5:**"
                            )
                        }]
                    }
                    
            except ValueError:
                # No es un número válido (respuesta ambigua)
                session_attributes["esperando_calificacion"] = "true"
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            f"🤔 No entendí tu respuesta '{calificacion_input}'.\n\n"
                            "Por favor, califica tu experiencia con un número del 1 al 5:\n\n"
                            "⭐ 1 estrella - Muy mala\n"
                            "⭐⭐ 2 estrellas - Mala\n"
                            "⭐⭐⭐ 3 estrellas - Regular\n"
                            "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                            "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                            "💬 **Responde solo con el número (1, 2, 3, 4 o 5):**"
                        )
                    }]
                }
                
        # PRIORIDAD 1.2: Manejar respuestas de información adicional PRIMERO
        respuesta_info = manejar_respuestas_info_adicional(session_attributes, input_transcript)
        if respuesta_info:
            return respuesta_info

        # PRIORIDAD 1.3: NUEVO - Detectar flujo de validación de documento
        if (session_attributes.get("en_flujo_activo") in ["ConsultaInfoPlan", "ConsultarInvitados", "FQAReferidos", "ConsultaIncapacidades", "CongelarPlan"] 
            and (not session_attributes.get("document_type_id") or not session_attributes.get("document_number"))
            and session_attributes.get("acepto_politicas") == "true"):
            
            print(f"🔍 DETECTADO: Usuario en flujo de validación de documento para {session_attributes.get('en_flujo_activo')}")
            print(f"🔍 Input del usuario: '{input_transcript}'")
            
            # Forzar el procesamiento como validación de documento
            
            intent_en_flujo = {
                "name": session_attributes.get("en_flujo_activo"),
                "slots": intent.get("slots", {}),
                "state": "InProgress"
            }
            
            # Validar documento usando el input actual
            document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                intent.get("slots", {}), session_attributes, input_transcript, intent_en_flujo
            )
            
            if respuesta_incompleta:
                print("📤 Retornando respuesta de validación de documento")
                return respuesta_incompleta
            
            # Si la validación está completa, continuar con el flujo original
            if document_type_id and document_number:
                print(f"✅ Validación completa! Continuando con {session_attributes.get('en_flujo_activo')}")
                # Forzar el intent_name para que procese correctamente
                intent_name = session_attributes.get("en_flujo_activo")
        
        # PRIORIDAD 1.4: Detectar consultas de clases grupales en texto libre (ANTES de fallback)
        if (session_attributes.get("acepto_politicas") == "true" 
            and intent_name in ["FallbackIntent", ""] 
            and not session_attributes.get("en_flujo_activo")
            and not session_attributes.get("esperando_seleccion_menu")
            and not session_attributes.get("esperando_respuesta_final")
            and not session_attributes.get("esperando_calificacion")):
            
            try:
                print(f"🔍 Analizando input para detección automática de grupales: '{input_transcript}'")
                
                # 🆕 VERIFICACIÓN ESPECIAL PARA CENTRO MAYOR
                if "centro mayor" in input_transcript.lower():
                    print("🎯 DETECCIÓN AUTOMÁTICA: 'centro mayor' encontrado - procesando como ConsultaGrupales")
                
                # Usar la función existente para extraer parámetros
                resultado_grupales = extraer_y_validar_slots_grupales(input_transcript, session_attributes, {
                    "name": "ConsultaGrupales",
                    "slots": {}
                })
                
                print(f"🔍 Resultado extraer_y_validar_slots_grupales: {resultado_grupales}")
                
                # CASO 1: Si la función devuelve una respuesta directa (pregunta), enviarla
                if (resultado_grupales and resultado_grupales.get("sessionState")):
                    print("✅ Función devolvió una pregunta - enviando respuesta directa")
                    return resultado_grupales
                
                # CASO 2: Si se detectaron parámetros válidos, procesar como ConsultaGrupales
                if (resultado_grupales 
                    and not resultado_grupales.get("error")
                    and (resultado_grupales.get("ciudad_id") or resultado_grupales.get("sede_id") or resultado_grupales.get("clase_id"))):
                    
                    print("✅ Parámetros de grupales detectados automáticamente!")
                    print(f"✅ Ciudad: {resultado_grupales.get('ciudad_nombre')}")
                    print(f"✅ Sede: {resultado_grupales.get('sede_nombre')}")
                    print(f"✅ Clase: {resultado_grupales.get('clase_nombre')}")
                    print(f"✅ Fecha: {resultado_grupales.get('fecha')}")
                    
                    # Forzar el intent a ConsultaGrupales
                    intent_name = "ConsultaGrupales"
                    intent = {"name": "ConsultaGrupales", "slots": {}}
                    
                    # Actualizar session_attributes con los datos detectados
                    if resultado_grupales.get("ciudad_id"):
                        session_attributes["ciudad_id"] = str(resultado_grupales["ciudad_id"])
                        session_attributes["ciudad_nombre"] = resultado_grupales["ciudad_nombre"]
                        intent["slots"]["ciudad"] = {
                            "value": {"interpretedValue": resultado_grupales["ciudad_nombre"]},
                            "shape": "Scalar"
                        }
                    
                    if resultado_grupales.get("sede_id"):
                        session_attributes["sede_id"] = str(resultado_grupales["sede_id"])
                        session_attributes["sede_nombre"] = resultado_grupales["sede_nombre"]
                        intent["slots"]["sede"] = {
                            "value": {"interpretedValue": resultado_grupales["sede_nombre"]},
                            "shape": "Scalar"
                        }
                    
                    if resultado_grupales.get("clase_id"):
                        session_attributes["clase_id"] = str(resultado_grupales["clase_id"])
                        session_attributes["clase_nombre"] = resultado_grupales["clase_nombre"]
                        intent["slots"]["clase"] = {
                            "value": {"interpretedValue": resultado_grupales["clase_nombre"]},
                            "shape": "Scalar"
                        }
                    
                    if resultado_grupales.get("fecha"):
                        intent["slots"]["fecha"] = {
                            "value": {"interpretedValue": resultado_grupales["fecha"]},
                            "shape": "Scalar"
                        }
                    
                    # Actualizar slots locales
                    slots = intent["slots"]
                    
                    print("🔄 Redirigiendo automáticamente a ConsultaGrupales con parámetros extraídos")
                
                else:
                    # Si no es grupales, intentar detectar sedes
                    print(f"🔍 No es grupales, intentando detectar sedes...")
                    
                    resultado_sedes = extraer_y_validar_slots_sedes(input_transcript, session_attributes, {
                        "name": "ConsultarSedes",
                        "slots": {}
                    })
                    
                    print(f"🔍 Resultado extraer_y_validar_slots_sedes: {resultado_sedes}")
                    
                    # CASO 1: Si la función devuelve una respuesta directa (pregunta), enviarla
                    if (resultado_sedes and resultado_sedes.get("sessionState")):
                        print("✅ Función sedes devolvió una pregunta - enviando respuesta directa")
                        return resultado_sedes
                    
                    # CASO 2: Si se detectaron parámetros válidos, procesar como ConsultarSedes
                    if (resultado_sedes 
                        and not resultado_sedes.get("error")
                        and (resultado_sedes.get("ciudad_id") or resultado_sedes.get("sede_id") or resultado_sedes.get("categoria_nombre"))):
                        
                        print("✅ Parámetros de sedes detectados automáticamente!")
                        print(f"✅ Ciudad: {resultado_sedes.get('ciudad_nombre')}")
                        print(f"✅ Sede: {resultado_sedes.get('sede_nombre')}")
                        print(f"✅ Categoría: {resultado_sedes.get('categoria_nombre')}")
                        print(f"✅ Tipo consulta: {resultado_sedes.get('tipo_consulta')}")
                        
                        # 🆕 CASO ESPECIAL: Si es consulta directa de horarios, manejar inmediatamente
                        if resultado_sedes.get("consulta_directa") and resultado_sedes.get("tipo_consulta") == "horarios_sede":
                            print("🎯 CONSULTA DIRECTA DE HORARIOS DETECTADA EN FALLBACK")
                            
                            sede_id = resultado_sedes["sede_id"]
                            sede_nombre = resultado_sedes["sede_nombre"]
                            ciudad_nombre = resultado_sedes["ciudad_nombre"]
                            
                            # Actualizar session_attributes
                            session_attributes.update(resultado_sedes.get("session_attributes", {}))
                            
                            horarios = consultar_horarios_sede(sede_id)
                            if not horarios:
                                mensaje_final = f"No se encontraron horarios para la sede {sede_nombre} en {ciudad_nombre}. 🕐"
                            else:
                                mensaje_final = respuesta_bedrock("ConsultarSedes", horarios)
                                if not mensaje_final or not mensaje_final.strip():
                                    mensaje_final = f"🏢 **Horarios de {sede_nombre}** en {ciudad_nombre}:\n\n📅 Consulta completada exitosamente."
                            
                            # Preguntar por más consultas
                            session_attributes["esperando_transicion_sedes"] = "true"
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                                    "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": (
                                        f"{mensaje_final}\n\n"
                                        "¿Deseas hacer otra consulta de sedes? 🏢\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n"
                                        "3️⃣ No gracias\n\n"
                                        "🏠 M Menú principal\n"
                                        "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                                    )
                                }]
                            }
                        
                        # Forzar el intent a ConsultarSedes
                        intent_name = "ConsultarSedes"
                        intent = {"name": "ConsultarSedes", "slots": {}}
                        
                        # Actualizar session_attributes con los datos detectados
                        if resultado_sedes.get("ciudad_id"):
                            session_attributes["ciudad_id"] = str(resultado_sedes["ciudad_id"])
                            session_attributes["ciudad_nombre"] = resultado_sedes["ciudad_nombre"]
                            intent["slots"]["ciudad"] = {
                                "value": {"interpretedValue": resultado_sedes["ciudad_nombre"]},
                                "shape": "Scalar"
                            }
                        
                        if resultado_sedes.get("sede_id"):
                            session_attributes["sede_id"] = str(resultado_sedes["sede_id"])
                            session_attributes["sede_nombre"] = resultado_sedes["sede_nombre"]
                            intent["slots"]["sede"] = {
                                "value": {"interpretedValue": resultado_sedes["sede_nombre"]},
                                "shape": "Scalar"
                            }
                        
                        if resultado_sedes.get("categoria_nombre"):
                            session_attributes["categoria_detectada"] = resultado_sedes["categoria_nombre"]
                            intent["slots"]["categoria"] = {
                                "value": {"interpretedValue": resultado_sedes["categoria_nombre"]},
                                "shape": "Scalar"
                            }
                        
                        # 🆕 MARCAR QUE LA DETECCIÓN AUTOMÁTICA SE COMPLETÓ EN FALLBACK
                        session_attributes["deteccion_automatica_completada"] = "true"
                        
                        # Actualizar slots locales
                        slots = intent["slots"]
                        
                        print("🔄 Redirigiendo automáticamente a ConsultarSedes con parámetros extraídos")
                        # NO hacer return aquí, dejar que continúe el flujo normal de ConsultarSedes
                
            except Exception as e:
                print(f"⚠️ Error en detección automática: {str(e)}")
                    
            
        # PRIORIDAD 1.5: Manejar transiciones de ConsultaGrupales ANTES que esperando_respuesta_final
        respuesta_grupales = esperando_respuesta_grupales(session_attributes, input_transcript, slots, intent)
        if respuesta_grupales:
            return respuesta_grupales

        # PRIORIDAD 1.7: Detectar transiciones de ConsultaGrupales ANTES de protección
        if (
            session_attributes.get("esperando_transicion_grupales") == "true"
            and input_transcript.strip() in ["1", "2", "3", "4", "5"]  # ACTUALIZADO: incluir 4 y 5
        ):
            
            print(f"🔍 FORZANDO ConsultaGrupales para transición: '{input_transcript}'")
            # Forzar el intent a ConsultaGrupales para que procese la transición
            intent_name = "ConsultaGrupales"
            intent = {"name": "ConsultaGrupales", "slots": slots}

        # PRIORIDAD 1.8: Procesar selección del menú principal (MÁXIMA PRIORIDAD)
        if session_attributes.get("esperando_seleccion_menu") == "true":
            print(f"🏠 PROCESANDO SELECCIÓN DE MENÚ PRINCIPAL (PRIORIDAD MÁXIMA): '{input_transcript}'")
            resultado_menu = procesar_seleccion_menu(input_transcript, session_attributes)
            
            # MANEJAR EL CASO CUANDO NECESITA CONTINUAR EL FLUJO
            if isinstance(resultado_menu, dict) and resultado_menu.get("continuar_flujo"):
                print("🔄 Menú retornó continuar_flujo - procesando flujo normal")
                intent_name = resultado_menu["intent_name"]
                session_attributes = resultado_menu["session_attributes"]
                intent = {"name": intent_name, "slots": {}}
                slots = {}
            else:
                # Si el menú retorna una respuesta normal, enviarla
                return resultado_menu

        # PRIORIDAD 2: Manejar esperando_respuesta_final segundo (pero excluyendo ConsultaGrupales)  
        if (session_attributes.get("esperando_respuesta_final") == "true" and 
              not session_attributes.get("esperando_info_invitados") and
              not session_attributes.get("esperando_info_incapacidad") and 
              not session_attributes.get("esperando_info_referidos") and
              not session_attributes.get("esperando_transicion_grupales") and
              input_transcript.strip() not in ["1", "2", "3", "4", "5"]):
            
            print("🔍 ===== DEBUG RESPUESTA FINAL =====")
            print(f"🔍 input_transcript: '{input_transcript}'")
            print(f"🔍 session_attributes: {session_attributes}")
            print("🔄 Procesando respuesta final...")
            
            # Limpiar la bandera inmediatamente para evitar bucles
            session_attributes.pop("esperando_respuesta_final", None)

            
            # Si Lex detectó una intención válida (NO FallbackIntent) deja que el flujo siga normalmente
            if intent_name not in ["FallbackIntent", "None", ""]:
                print(f"🔍 Lex detectó intención válida: {intent_name}, dejando que el flujo siga normalmente")
                #  CORRECCIÓN: Si la intención es ConsultaHorarios pregunta por tipo_horario de una
                if intent_name == "ConsultaHorarios":
                    print("🔄 Redirigiendo a ConsultaHorarios tras pregunta final, preguntando tipo_horario")
                    session_attributes.pop("esperando_respuesta_final", None)
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_horario"},
                            "intent": {
                                "name": "ConsultaHorarios",
                                "slots": {},
                                "state": "InProgress"
                            },
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                "¿Qué tipo de horarios deseas consultar? 📅\n\n"
                                "🏃‍♂️ **Clases grupales** - Horarios de actividades específicas\n"
                                "🏢 **Sede** - Horarios de atención general\n\n"
                                "💬 **Responde:** 'Clases grupales' o 'Sede'"
                            )
                        }]
                    }
            # Normalizar input para detectar respuestas
            input_normalizado = unicodedata.normalize('NFKD', input_transcript.lower()).encode('ascii', 'ignore').decode('ascii')
            input_normalizado = input_normalizado.encode('ascii', 'ignore').decode('ascii')
            input_normalizado = re.sub(r'[^\w\s]', '', input_normalizado)
            
            # Mapeo de respuestas comunes
            mapeo_respuestas = {
                's': 'si',
                'sued': 'si',
                'sud': 'si',
                'sd': 'si'
            }
            if input_normalizado.strip() in mapeo_respuestas:
                input_normalizado = mapeo_respuestas[input_normalizado.strip()]
            
            print(f"🔍 input_normalizado: '{input_normalizado}'")
            
            # DETECTAR RESPUESTAS NEGATIVAS (ir a calificación)
            if any(p in input_normalizado for p in ["no", "nada", "gracias", "eso es todo", "ninguna", "no gracias", "nada mas"]):
                print("🔍 Usuario dijo NO - enviando a calificación")
                
                # Limpiar toda la sesión
                keys_to_remove = [
                    "en_flujo_activo", "clase_display", "slots_previos",
                    "ciudad_nombre", "sede_nombre", "ciudad_id", "sede_id", "esperando_transicion_grupales",
                    "esperando_info_invitados", "esperando_info_incapacidad", "esperando_info_referidos",
                    "preguntando_otro_documento", "cambiando_documento"
                ]
                for key in keys_to_remove:
                    session_attributes.pop(key, None)
                session_attributes.pop("esperando_respuesta_final", None)
                session_attributes["esperando_calificacion"] = "true"
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            "¡Gracias por usar nuestro servicio! 🌟\n\n"
                            "¿Podrías calificar tu experiencia?\n\n"
                            "⭐ 1 estrella - Muy mala\n"
                            "⭐⭐ 2 estrellas - Mala\n"
                            "⭐⭐⭐ 3 estrellas - Regular\n"
                            "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                            "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                            "💬 **Responde con un número del 1 al 5:**"
                        )
                    }]
                }
            
            # DETECTAR RESPUESTAS AFIRMATIVAS (continuar ayuda)
            elif any(p in input_normalizado for p in ["si", "yes", "claro", "vale", "ok", "por supuesto", "ayuda", "ayudar", "necesito", "quiero"]):
                print("🔍 Usuario dijo SÍ - mostrando menú principal")
                
                # Limpiar datos de sesión pero mantener algunos básicos
                keys_to_remove = [
                    "en_flujo_activo", "clase_display", "slots_previos",
                    "esperando_transicion_grupales", "esperando_info_invitados", 
                    "esperando_info_incapacidad", "esperando_info_referidos"
                    # ❌ NO remover "intenciones_con_documento", "document_type_id", "document_number"
                ]
                for key in keys_to_remove:
                    session_attributes.pop(key, None)
                
                #  guardar historial de intenciones
                print(f"🔍 Preservando intenciones_con_documento: {session_attributes.get('intenciones_con_documento')}")
                return mostrar_menu_principal(session_attributes)
            
            # interceptar SaludoHabeasData
            elif intent_name == "SaludoHabeasData":
                print("🔍 Interceptando SaludoHabeasData tras pregunta final, mostrando menú de ayuda")
                keys_to_remove = [
                    "en_flujo_activo", "clase_display", "slots_previos",
                    "esperando_transicion_grupales", "esperando_info_invitados", 
                    "esperando_info_incapacidad", "esperando_info_referidos"
                ]
                for key in keys_to_remove:
                    session_attributes.pop(key, None)
                
                return mostrar_menu_principal(session_attributes)
                
            if intent_name in ["ConsultarSedes"]:
                print(f"🔄 Redirigiendo flujo directo a {intent_name} por input: '{input_transcript}'")
                # Pregunta por ciudad si no está presente
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                        "intent": {
                            "name": intent_name,
                            "slots": {
                                "ciudad": {
                                    "value": {
                                        "originalValue": "",
                                        "resolvedValues": [],
                                        "interpretedValue": ""
                                    },
                                    "shape": "Scalar"
                                }
                            },
                            "state": "InProgress"
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿En qué ciudad deseas consultar los horarios de sede?"
                    }]
                }
                
            
            # DETECTAR INTENCIONES ESPECÍFICAS (flujo normal)
            else:
                print("🔍 Usuario mencionó una intención específica - verificando...")
                
                try:
                    # Clasificar con Bedrock
                    prompt = f"""
                        Usuario dijo: "{input_transcript}",
                        instrucciones: (
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
                            Si la frase ES claramente una de estas intenciones, responde SOLO con el nombre de la intención.\n"
                            Si NO es clara, responde: "No detectado"
                        """
                    
                    
                    intencion_detectada = consultar_bedrock_generacion(prompt).strip()
                    intenciones_validas = [
                        "ConsultaHorarios", "ConsultaGrupales", "ConsultarSedes", "FQABodytech", "Venta", "ConsultaInfoPlan",
                        "ConsultarInvitados", "ConsultaIncapacidades", "FQAReferidos", "CongelarPlan"
                    ]
                    if intencion_detectada == "ConsultaHorarios":
                        print(f"✅ Intención detectada: {intencion_detectada} (preguntar tipo de horario)")
                        # Limpiar sesión y disparar la intención ConsultaHorarios preguntando tipo_horario
                        for key in [
                            "en_flujo_activo", "clase_display", "slots_previos", "esperando_transicion_grupales",
                            "esperando_info_invitados", "esperando_info_incapacidad", "esperando_info_referidos",
                            "esperando_respuesta_final"
                        ]:
                            session_attributes.pop(key, None)
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_horario"},
                                "intent": {
                                    "name": "ConsultaHorarios",
                                    "slots": {},
                                    "state": "InProgress"
                                },
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    "¿Qué tipo de horarios deseas consultar? 📅\n\n"
                                    "🏃‍♂️ **Clases grupales** - Horarios de actividades específicas\n"
                                    "🏢 **Sede** - Horarios de atención general\n\n"
                                    "💬 **Responde:** 'Clases grupales' o 'Sede'"
                                )
                            }]
                        }
                    elif intencion_detectada in intenciones_validas:
                        print(f"🔄 Llamando recursivamente a {intencion_detectada}")
                        # Limpiar sesión y disparar la nueva intención
                        keys_to_remove = [
                            "en_flujo_activo", "clase_display", "slots_previos", "esperando_transicion_grupales",
                            "esperando_info_invitados", "esperando_info_incapacidad", "esperando_info_referidos",
                            "esperando_respuesta_final"
                        ]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        nuevo_event = {
                            **event,
                            "sessionState": {
                                **event["sessionState"],
                                "intent": {
                                    "name": intencion_detectada,
                                    "slots": {},
                                    "state": "ReadyForFulfillment"
                                },
                                "sessionAttributes": session_attributes
                            }
                        }
                        return lambda_handler(nuevo_event, context)
                    else:
                        print(f"❌ No se detectó intención válida: {intencion_detectada}")
                        raise Exception("No es una intención válida")
                        
                except Exception as e:
                    print(f"⚠️ Error en clasificación o no es intención: {str(e)}")
                    
                    # Si no es una intención válida, IR A CALIFICACIÓN EN LUGAR DE SUGERENCIAS
                    print("🔍 Respuesta ambigua - enviando a calificación")
                    
                    # Limpiar toda la sesión  
                    keys_to_remove = [
                        "en_flujo_activo", "clase_display", "slots_previos",
                        "ciudad_nombre", "sede_nombre", "ciudad_id", "sede_id", "esperando_transicion_grupales",
                        "esperando_info_invitados", "esperando_info_incapacidad", "esperando_info_referidos",
                        "preguntando_otro_documento", "cambiando_documento"
                    ]
                    for key in keys_to_remove:
                        session_attributes.pop(key, None)
                    session_attributes["esperando_calificacion"] = "true"
                    
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitIntent"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                "¡Gracias por usar nuestro servicio! 🌟\n\n"
                                "¿Podrías calificar tu experiencia?\n\n"
                                "⭐ 1 estrella - Muy mala\n"
                                "⭐⭐ 2 estrellas - Mala\n"
                                "⭐⭐⭐ 3 estrellas - Regular\n"
                                "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                                "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                                "💬 **Responde con un número del 1 al 5:**"
                            )
                        }]
                    }
        
        # PRIORIDAD 2.5: Manejar respuesta final después de ConsultaGrupales
        elif (session_attributes.get("ultimo_intent_completado") == "ConsultaGrupales" and
              not session_attributes.get("esperando_transicion_grupales") and
              not session_attributes.get("en_flujo_activo") and
              intent_name == "SaludoHabeasData"):
            
            # Limpiar la bandera inmediatamente para evitar bucles
            session_attributes.pop("esperando_respuesta_final", None)
            
            # Normalizar input para detectar respuestas
            input_normalizado = unicodedata.normalize('NFKD', input_transcript.lower()).encode('ascii', 'ignore').decode('ascii')
            input_normalizado = input_normalizado.encode('ascii', 'ignore').decode('ascii')
            input_normalizado = re.sub(r'[^\w\s]', '', input_normalizado)
            
            # Mapeo de respuestas comunes
            mapeo_respuestas = {
                's': 'si',
                'sued': 'si',
                'sud': 'si',
                'sd': 'si'
            }
            if input_normalizado.strip() in mapeo_respuestas:
                input_normalizado = mapeo_respuestas[input_normalizado.strip()]
            
            print(f"🔍 input_normalizado: '{input_normalizado}'")
            
            # DETECTAR RESPUESTAS NEGATIVAS (ir a calificación)
            if any(p in input_normalizado for p in ["no", "nada", "gracias", "eso es todo", "ninguna", "no gracias", "nada mas"]):
                print("🔍 Usuario dijo NO - enviando a calificación")
                
                # Limpiar toda la sesión
                keys_to_remove = [
                    "en_flujo_activo", "clase_display", "slots_previos",
                    "ciudad_nombre", "sede_nombre", "ciudad_id", "sede_id", "esperando_transicion_grupales"
                ]
                for key in keys_to_remove:
                    session_attributes.pop(key, None)
                session_attributes["esperando_calificacion"] = "true"
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                    "¡Gracias por usar nuestro servicio! 🌟\n\n"
                    "¿Podrías calificar tu experiencia?\n\n"
                    "⭐ 1 estrella - Muy mala\n"
                    "⭐⭐ 2 estrellas - Mala\n"
                    "⭐⭐⭐ 3 estrellas - Regular\n"
                    "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                    "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                    "💬 **Responde con un número del 1 al 5:**"
                )
                    }]
                }
            
            # DETECTAR RESPUESTAS AFIRMATIVAS (continuar ayuda)
            elif any(p in input_normalizado for p in ["si", "yes", "claro", "vale", "ok", "por supuesto", "ayuda", "ayudar", "necesito", "quiero"]):
                print("🔍 Usuario dijo SÍ - mostrando menú principal")
                
                # Limpiar datos de sesión pero mantener algunos básicos
                keys_to_remove = [
                    "en_flujo_activo", "clase_display", "slots_previos",
                    "esperando_transicion_grupales"
                ]
                for key in keys_to_remove:
                    session_attributes.pop(key, None)
                
                return mostrar_menu_principal(session_attributes)
            
            # DETECTAR INTENCIONES ESPECÍFICAS (flujo normal)
            else:
                print("🔍 Usuario mencionó una intención específica - verificando...")
                
                try:
                    # Clasificar con Bedrock
                    prompt = f"""
Usuario dijo: "{input_transcript}"

IMPORTANTE: Solo clasifica como intención válida si el mensaje tiene SENTIDO CLARO y se relaciona obviamente con los temas listados.

NO clasifiques como intención válida si:
- Es texto sin sentido o caracteres aleatorios
- Son solo letras mezcladas sin significado  
- Es tecleo accidental
- No se entiende qué quiere el usuario
- Contiene secuencias de teclado como 'asdf', 'qwerty', etc.
- Tiene muchas consonantes seguidas sin vocales
- Son caracteres repetitivos sin sentido

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

Si la frase ES claramente una de estas intenciones, responde SOLO con el nombre de la intención.
Si NO es clara, es confusa, o es texto sin sentido, responde: "No encontramos esta consulta, pero puedo ayudarte en otras cosas como preguntas frecuentes, ventas, consultas de plan, invitados, incapacidades, referidos, clases grupales, sedes o congelaciones."
"""
                    
                    intencion_detectada = consultar_bedrock_generacion(prompt).strip()
                    
                    intenciones_validas = [
                        "FQABodytech", "Venta", "ConsultaInfoPlan", "ConsultarInvitados", 
                        "ConsultaIncapacidades", "FQAReferidos", "ConsultaGrupales", 
                        "ConsultarSedes", "CongelarPlan"
                    ]
                    
                    if intencion_detectada in intenciones_validas:
                        print(f"✅ Intención detectada: {intencion_detectada}")
                        intencion_detectada = "No detectado"
                        # Limpiar sesión y disparar la nueva intención
                        keys_to_remove = [
                            "en_flujo_activo", "clase_display", "slots_previos", "esperando_transicion_grupales"
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
                                "❄️ Consultar congelaciones\n\n"
                                "¿Sobre cuál tema necesitas ayuda?"
                            )
                        }]
                    }
                    
        respuesta_transicion_sedes = esperando_respuesta_sedes(session_attributes, input_transcript, slots, intent)
        if respuesta_transicion_sedes:
            return respuesta_transicion_sedes
        
                
        # PRIORIDAD 3: Protección de intenciones TERCERO (después de manejar respuestas)
        if (flujo_activo and flujo_activo in intenciones_protegidas and 
            intent_name != flujo_activo and intent_name in intenciones_que_interrumpen and
            # no interrumpir si hay información adicional pendiente
            
            not any([
                session_attributes.get("esperando_info_invitados") == "true",
                session_attributes.get("esperando_info_incapacidad") == "true", 
                session_attributes.get("esperando_info_referidos") == "true"
            ]) and
            # **NUEVA CONDICIÓN**: No proteger si dice "todas" en ConsultarSedes
            not (flujo_activo == "ConsultarSedes" and 
                 any(palabra in input_transcript.lower() for palabra in ["todas", "ver todas", "mostrar todas"])) and
            # **NUEVA CONDICIÓN**: No proteger si está esperando selección del menú principal
            session_attributes.get("esperando_seleccion_menu") != "true"):
            
            # ========================================
            # PROTECCIÓN ESPECÍFICA PARA CONSULTARSEDES
            # ========================================
            if flujo_activo == "ConsultarSedes":
                print("🔒 Protegiendo ConsultarSedes - retomando flujo específico")
                
                if (
                    session_attributes.get("pregunta_categoria") == "pendiente"
                    and any(
                        palabra in input_transcript.lower()
                        for palabra in ["todas", "ver todas", "mostrar todas"]
                    )
                ):
                    print("🔓 EXCEPCIÓN: Usuario dice 'todas' - permitiendo flujo normal")
                    # NO hacer return aquí, continuar con el flujo normal
                else:
                    # Resto del código de protección existente...
                    respuesta_rapida = "Te ayudaré con eso después de completar tu consulta actual."

                    # Determinar el estado actual del flujo
                    esperando_transicion = session_attributes.get("esperando_transicion_sedes") == "true"
                    consultando_horarios = session_attributes.get("consultando_horarios")
                    pregunta_categoria = session_attributes.get("pregunta_categoria")
                    ciudad = session_attributes.get("ciudad")
                
                # 1. Si está esperando transición, mantener ese estado
                if esperando_transicion:
                    print("🔍 Retomando: esperando transición de sedes")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"{respuesta_rapida} Continuemos con tu consulta de sedes.\n\n¿Deseas hacer otra consulta de sedes? 🏢\n\nSelecciona una opción:\n1️⃣ Otra ciudad\n2️⃣ Otra sede\n3️⃣ No gracias\n\n🏠 M Menú principal\n💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                        }]
                    }
                
                # 2. Si está eligiendo sede para horarios
                elif consultando_horarios == "eligiendo_sede":
                    print("🔍 Retomando: eligiendo sede para horarios")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "Sede"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"{respuesta_rapida} Continuemos con tu consulta de sedes.\n\n¿De cuál sede deseas consultar los horarios?"
                        }]
                    }
                
                # 3. Si está preguntando por horarios
                elif consultando_horarios == "preguntando":
                    print("🔍 Retomando: preguntando por horarios")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"{respuesta_rapida} Continuemos con tu consulta de sedes.\n\n¿Deseas consultar los horarios de alguna sede específica? 🕐\n\n"
                            "Si deseas consultar los horarios, escribe el nombre de la sede directamente.\n"
                            "Si no deseas consultar los horarios de una sede, responde con 'No' o marca la opción 2.\n\n"
                            "💬 *Ejemplo:* 'Chapinero' o 'No'"
                        }]
                    }
                
                # 4. Si está preguntando por categoría
                elif pregunta_categoria == "pendiente":
                    print("🔍 Retomando: preguntando por categoría")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"{respuesta_rapida} Continuemos con tu consulta de sedes.\n\n¿Cómo te gustaría ver las sedes?\n\n🎯 **'Por categoría'** - Te muestro las sedes organizadas por tipo\n📋 **'Todas'** - Te muestro todas las sedes de una vez\n\n💬 **Responde:** 'Por categoría' o 'Todas'"
                        }]
                    }
                
                # 5. Si necesita elicitar categoría
                elif pregunta_categoria == "si":
                    print("🔍 Retomando: elicitando categoría")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "categoria"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"{respuesta_rapida} Continuemos con tu consulta de sedes.\n\n¿Cuál categoría deseas consultar?"
                        }]
                    }
                
                # 6. Si no tiene ciudad, elicitar ciudad
                elif not ciudad:
                    print("🔍 Retomando: elicitando ciudad")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"{respuesta_rapida} Continuemos con tu consulta de sedes.\n\n¿En qué ciudad deseas consultar las sedes?"
                        }]
                    }
                
                # 7. Estado por defecto
                else:
                    print("🔍 Retomando: estado por defecto de ConsultarSedes")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitIntent"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": f"{respuesta_rapida} Continuemos con tu consulta de sedes."
                        }]
                    }
            
            # ========================================
            # PROTECCIÓN PARA OTRAS INTENCIONES
            # ========================================
            elif flujo_activo == "ConsultaGrupales":
                # ... código existente para ConsultaGrupales ...
                ciudad_actual = get_slot_value(slots_originales, "ciudad") or session_attributes.get("ciudad")
                sede_actual = get_slot_value(slots_originales, "sede")     
                clase_actual = get_slot_value(slots_originales, "clase")   
                fecha_actual = get_slot_value(slots_originales, "fecha")
                
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
            
            # ========================================
            # PROTECCIÓN GENÉRICA PARA OTRAS INTENCIONES
            # ========================================
            else:
                
                # verificar flujo especial
                if session_attributes.get("flujo_otra_sede") == "true":
                   print("🔍 Permitiendo continuar flujo de otra sede...")
                   session_attributes.pop("flujo_otra_sede", None)
                # Mensajes específicos por intención
                if intent_name == "FQABodytech":
                    respuesta_rapida = "Bodytech es un centro médico deportivo que ofrece servicios de salud y bienestar."
                elif intent_name == "Venta":
                    respuesta_rapida = "Para información sobre ventas, te conectaremos con un asesor al finalizar tu consulta actual."
                elif intent_name == "ConsultarInvitados":
                    respuesta_rapida = "Te ayudaré con tus invitados después de completar tu consulta actual."
                elif intent_name == "ConsultaInfoPlan":
                    respuesta_rapida = "Te ayudaré con la información de tu plan después de completar tu consulta actual."
                elif intent_name == "ConsultaIncapacidades":
                    respuesta_rapida = "Te ayudaré con tus incapacidades después de completar tu consulta actual."
                elif intent_name == "FQAReferidos":
                    respuesta_rapida = "Te ayudaré con tus referidos después de completar tu consulta actual."
                elif intent_name == "CongelarPlan":
                    respuesta_rapida = "Te ayudaré con la congelación de tu plan después de completar tu consulta actual."
                elif intent_name == "Ingresos":
                    respuesta_rapida = "Te ayudaré con la consulta de ingresos después de completar tu consulta actual."
                else:
                    respuesta_rapida = "Te ayudaré con eso después de completar tu consulta actual."
                
                # Reconstruir slots originales
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
                mensaje_continuacion = "Continuemos con tu consulta actual."
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "intent": {
                            "name": flujo_activo,
                            "slots": {},
                            "state": "InProgress"
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": f"{respuesta_rapida} {mensaje_continuacion}"
                    }]
                }
            
              
        intents_requieren_doc = {"ConsultaInfoPlan", "ConsultarInvitados", "ConsultaIncapacidades", "FQAReferidos", "CongelarPlan"}
        
        # Debug detallado para rastrear el problema
        if intent_name in intents_requieren_doc:
            print(f"🔍 ===== DEBUG INTENCIONES CON DOCUMENTO =====")
            print(f"🔍 Intent actual: {intent_name}")
            print(f"🔍 intenciones_con_documento antes: '{session_attributes.get('intenciones_con_documento', '')}'")
            print(f"🔍 preguntando_otro_documento: {session_attributes.get('preguntando_otro_documento')}")
            print(f"🔍 cambiando_documento: {session_attributes.get('cambiando_documento')}")
            
            intenciones_set.add(intent_name)
            session_attributes["intenciones_con_documento"] = ",".join(intenciones_set)
            
            print(f"🔍 intenciones_set después: {intenciones_set}")
            print(f"🔍 Longitud intenciones_set: {len(intenciones_set)}")
            print(f"🔍 ===============================================")

            # Mejorar la condición de validación
            if (
                len(intenciones_set) > 1
                and not session_attributes.get("preguntando_otro_documento")
                and not session_attributes.get("cambiando_documento")
                and session_attributes.get("acepto_politicas") == "true" 
            ):
                print(f"🔍 ✅ ACTIVANDO pregunta de otro documento para {intent_name}")
                session_attributes["preguntando_otro_documento"] = "true"
                session_attributes["cambiando_documento"] = ""
                session_attributes["intencion_tras_documento"] = intent_name  # Guardar la intención original
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "document_number"},
                        "intent": {
                            "name": intent_name,
                            "state": "InProgress",
                            "slots": {}  
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¿Quieres consultar con otro documento o seguir usando el que ya indicaste?\n\n"
                        "💬 **Puedes Decirme:** 'Otro documento' o 'Mismo documento'\n\n"
                    }]
                }
            else:
                print(f"🔍 ❌ NO activando pregunta de otro documento:")
                print(f"🔍   - len(intenciones_set) > 1: {len(intenciones_set) > 1}")
                print(f"🔍   - not preguntando_otro_documento: {not session_attributes.get('preguntando_otro_documento')}")
                print(f"🔍   - not cambiando_documento: {not session_attributes.get('cambiando_documento')}")
                print(f"🔍   - acepto_politicas: {session_attributes.get('acepto_politicas')}")

        if session_attributes.get("preguntando_otro_documento") == "true":
            print(f"🔍 DETECTADO: Usuario está respondiendo a pregunta de cambio documento")
            print(f"🔍 Input del usuario: '{input_transcript}'")
            
            input_lower = input_transcript.lower().strip()
            
            # Detectar respuestas de "otro documento"
            if any(palabra in input_lower for palabra in ["otro", "nuevo", "diferente", "cambiar", "si", "sí"]):
                print("✅ Usuario eligió OTRO documento - limpiando datos")
                
                # Limpiar completamente los datos de documento
                keys_documento_to_remove = [
                    "document_type_id", "document_type_raw", "document_number", 
                    "document_type", "preguntando_otro_documento", "cambiando_documento",
                    "intencion_tras_documento", "datos_plan_json", "info_plan",
                    "intenciones_con_documento"
                ]
                
                for key in keys_documento_to_remove:
                    session_attributes.pop(key, None)
                
                print("🧹 Datos de documento Y historial de intenciones completamente limpiados")
                
                # Mantener solo la intención actual y flujo activo
                intencion_actual = intent_name
                session_attributes["en_flujo_activo"] = intencion_actual
                
                # REESTABLECER EL HISTORIAL CON SOLO LA INTENCIÓN ACTUAL
                session_attributes["intenciones_con_documento"] = intencion_actual

                # Limpiar slots completamente para forzar recolección
                intent["slots"] = {}
                
                print(f"🔄 Reiniciando flujo para {intencion_actual}")
                
                
                document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                    {},  # slots vacíos para forzar la recolección desde cero
                    session_attributes,
                    "",  # input_transcript vacío para que pregunte desde el inicio
                    {
                        "name": intencion_actual,
                        "slots": {},
                        "state": "InProgress"
                    }
                )
                
                # Si necesita recolectar datos, retornar la respuesta de validación
                if respuesta_incompleta:
                    print("📤 Retornando respuesta para recolectar nuevo documento")
                    return respuesta_incompleta
                
                print("⚠️ Caso inesperado: ya tiene datos después de limpiar")
                
            elif any(palabra in input_lower for palabra in ["mismo", "continuar", "mantener", "no"]):
                print("✅ Usuario eligió MISMO documento - continuando")
                
                # Limpiar solo las banderas de cambio
                session_attributes.pop("preguntando_otro_documento", None)
                session_attributes.pop("cambiando_documento", None)
                
                # Obtener la intención original desde session_attributes
                intencion_original = session_attributes.get("intencion_tras_documento", intent_name)
                print(f"🎯 Continuando con intención original: {intencion_original}")
                
                # Forzar procesamiento de la intención con los datos existentes
                intent_name = intencion_original
                session_attributes["en_flujo_activo"] = intent_name
                
                # Continuar con el flujo normal (no hacer return aquí)
                print("🔄 Continuando con flujo normal de la intención")
                
            else:
                print(f"❌ Respuesta ambigua para cambio de documento: '{input_transcript}'")
                
                # Preguntar de nuevo más claramente
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "document_number"},
                        "intent": {
                            "name": intent_name,
                            "state": "InProgress",
                            "slots": {}
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            "🤔 No entendí tu respuesta.\n\n"
                            "¿Quieres consultar con **otro documento** diferente "
                            "o seguir usando el documento que ya indicaste?\n\n"
                            "💬 **Responde claramente:** 'Otro documento' o 'Mismo documento'"
                        )
                    }]
                }
        
        # Si el usuario responde explícitamente que quiere cambiar de documento:
        if slots and slots.get("cambiar_documento"):
            valor = slots["cambiar_documento"].get("value", {}).get("interpretedValue", "").lower()
            print(f"🔍 Procesando cambiar_documento: '{valor}'")
            
            if "otro" in valor or "nuevo" in valor or "diferente" in valor or "si" in valor or "sí" in valor:
                print("✅ Usuario eligió OTRO documento - limpiando datos")
                # Limpia los datos de documento en sesión
                keys_documento_to_remove = [
                    "document_type_id", "document_type_raw", "document_number", 
                    "document_type", "preguntando_otro_documento", "cambiando_documento",
                    "intencion_tras_documento", "datos_plan_json", "info_plan",
                    "intenciones_con_documento"
                ]
                
                for key in keys_documento_to_remove:
                    session_attributes.pop(key, None)
                
                print("🧹 Datos de documento Y historial de intenciones completamente limpiados")
                
                #  MANTENER SOLO LA INTENCIÓN ACTUAL Y FLUJO ACTIVO
                intencion_actual = intent_name
                session_attributes["en_flujo_activo"] = intencion_actual

                # REESTABLECER EL HISTORIAL CON SOLO LA INTENCIÓN ACTUAL
                session_attributes["intenciones_con_documento"] = intencion_actual
                
                #  LIMPIAR SLOTS COMPLETAMENTE PARA FORZAR RECOLECCIÓN
                intent["slots"] = {}
                
                print(f"🔄 Reiniciando flujo para {intencion_actual}")
                
                # LLAMAR A VALIDAR_DOCUMENTO_USUARIO DESDE CERO
                
                document_type_id, document_number, session_attributes, respuesta_incompleta = validar_documento_usuario(
                    {},  # slots vacíos para forzar la recolección desde cero
                    session_attributes,
                    "",  # input_transcript vacío para que pregunte desde el inicio
                    {
                        "name": intencion_actual,
                        "slots": {},
                        "state": "InProgress"
                    }
                )
                
                # Si necesita recolectar datos, retornar la respuesta de validación
                if respuesta_incompleta:
                    print("📤 Retornando respuesta para recolectar nuevo documento")
                    return respuesta_incompleta
                
                # Si por alguna razón ya tiene datos (no debería pasar), continuar
                print("⚠️ Caso inesperado: ya tiene datos después de limpiar")
                
            elif "mismo" in valor or "continuar" in valor or "mantener" in valor or "no" in valor:
                print("✅ Usuario eligió MISMO documento - continuando")
                
                #  LIMPIAR SOLO LAS BANDERAS DE CAMBIO
                session_attributes.pop("preguntando_otro_documento", None)
                session_attributes.pop("cambiando_documento", None)
                
                # Obtener la intención original desde session_attributes
                intencion_original = session_attributes.get("intencion_tras_documento", intent_name)
                print(f"🎯 Continuando con intención original: {intencion_original}")
                
                # ✅ FORZAR PROCESAMIENTO DE LA INTENCIÓN CON LOS DATOS EXISTENTES
                intent_name = intencion_original
                session_attributes["en_flujo_activo"] = intent_name
                
                # Continuar con el flujo normal (no hacer return aquí)
                print("🔄 Continuando con flujo normal de la intención")
                
            else:
                print(f"❌ Valor no reconocido para cambiar_documento: '{valor}'")
                
                # Preguntar de nuevo más claramente
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "document_number"},
                        "intent": {
                            "name": intent_name,
                            "state": "InProgress",
                            "slots": {}
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            "🤔 No entendí tu respuesta.\n\n"
                            "¿Quieres consultar con otro documento o seguir usando el que ya indicaste?\n\n"
                            "✅ **Respuestas válidas:**\n"
                            "🔸 **'Otro documento'** → Para usar documento diferente\n"
                            "🔸 **'Mismo documento'** → Para continuar con el actual\n\n"
                            "💬 **Escribe tu preferencia:**"
                        )
                    }]
                }
                      
        if session_attributes.get("esperando_calificacion") == "true":
            session_attributes.pop("esperando_calificacion", None)
            
            # Extraer número del input
            calificacion_input = input_transcript.strip()
            
            # Verificar si es un número válido del 1 al 5
            try:
                calificacion = int(calificacion_input)
                
                if 1 <= calificacion <= 5:
                    # Crear mensaje con estrellas
                    estrellas = "⭐" * calificacion
                    
                    # Mensaje personalizado según la calificación
                    if calificacion == 5:
                        mensaje_agradecimiento = f"¡Excelente! {estrellas}\n\n¡Nos alegra saber que tuviste una experiencia fantástica! 😊"
                    elif calificacion == 4:
                        mensaje_agradecimiento = f"¡Muy buena! {estrellas}\n\n¡Gracias por tu valoración positiva! 😊"
                    elif calificacion == 3:
                        mensaje_agradecimiento = f"Regular {estrellas}\n\n¡Gracias por tu calificación! Trabajaremos para mejorar. 😊"
                    elif calificacion == 2:
                        mensaje_agradecimiento = f"Mala {estrellas}\n\n¡Gracias por tu honestidad! Nos ayuda a mejorar nuestro servicio. 😊"
                    else:  # calificacion == 1
                        mensaje_agradecimiento = f"Muy mala {estrellas}\n\n¡Gracias por tu feedback! Tomaremos medidas para mejorar. 😊"
                    session_attributes["conversacion_finalizada"] = "true"
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
                            "content": f"{mensaje_agradecimiento}\n\n¡Que tengas un excelente día! 🌟"
                        }]
                    }
                else:
                    # Número fuera del rango 1-5
                    session_attributes["esperando_calificacion"] = "true"
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitIntent"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                f"🤔 '{calificacion_input}' no es una calificación válida.\n\n"
                                "Por favor, selecciona un número del 1 al 5:\n\n"
                                "⭐ 1 estrella - Muy mala\n"
                                "⭐⭐ 2 estrellas - Mala\n"
                                "⭐⭐⭐ 3 estrellas - Regular\n"
                                "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                                "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                                "💬 **Responde con un número del 1 al 5:**"
                            )
                        }]
                    }
                    
            except ValueError:
                # No es un número válido (respuesta ambigua)
                session_attributes["esperando_calificacion"] = "true"
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            f"🤔 No entendí tu respuesta '{calificacion_input}'.\n\n"
                            "Por favor, califica tu experiencia con un número del 1 al 5:\n\n"
                            "⭐ 1 estrella - Muy mala\n"
                            "⭐⭐ 2 estrellas - Mala\n"
                            "⭐⭐⭐ 3 estrellas - Regular\n"
                            "⭐⭐⭐⭐ 4 estrellas - Buena\n"
                            "⭐⭐⭐⭐⭐ 5 estrellas - Excelente\n\n"
                            "💬 **Responde solo con el número (1, 2, 3, 4 o 5):**"
                        )
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
            # No mostrar mensaje aquí, se maneja en el handler principal

        # -----------------------------
        # FLUJO: Consultar Actividades
        # -----------------------------

        if intent_name == "ConsultaGrupales":
            print("=== INICIO ConsultaGrupales ===")
            print("session_attributes al inicio:", session_attributes)
            print("slots al inicio:", slots)
            try:
                # ✅ RESETEAR CONTADOR AL INICIAR FLUJO EXITOSO
                from utils import resetear_contador_no_reconocidas
                resetear_contador_no_reconocidas(session_attributes)
                
                # USAR INPUT ORIGINAL DEL MENÚ SI EXISTE
                input_para_procesar = input_transcript

                # Si viene del menú principal, usar el input original que contenía los parámetros
                if session_attributes.get("input_original_menu"):
                    input_para_procesar = session_attributes.get("input_original_menu")
                    print(f"🔍 Usando input original del menú: '{input_para_procesar}'")
                    # Limpiar el input original para evitar confusiones futuras
                    session_attributes.pop("input_original_menu", None)

                input_completo = input_para_procesar
                if session_attributes.get("input_pendiente"):
                    input_completo = f"{session_attributes['input_pendiente']} {input_para_procesar}"
                    session_attributes.pop("input_pendiente", None)
                print("input_completo:", input_completo)
                resultado = extraer_y_validar_slots_grupales(input_completo, session_attributes, intent)
                print("resultado extraer_y_validar_slots_grupales:", resultado)
                
                # Manejar extracción exitosa sin consulta directa
                if resultado.get("ciudad_id") and not resultado.get("consulta_directa"):
                    print("✅ Ciudad extraída del input, continuando con flujo normal")
                    
                    # Actualizar session_attributes con los datos extraídos
                    session_attributes.update(resultado.get("session_attributes", {}))
                    
                    # Poblar el slot de ciudad en el intent
                    intent["slots"]["ciudad"] = {
                        "value": {"interpretedValue": resultado["ciudad_nombre"]},
                        "shape": "Scalar"
                    }
                    slots["ciudad"] = intent["slots"]["ciudad"]
                    
                    # ✅ NUEVO: Poblar el slot de sede si ya fue extraída
                    if resultado.get("sede_id"):
                        print(f"✅ Sede ya extraída: {resultado['sede_nombre']} (ID: {resultado['sede_id']})")
                        intent["slots"]["sede"] = {
                            "value": {"interpretedValue": resultado["sede_nombre"]},
                            "shape": "Scalar"
                        }
                        slots["sede"] = intent["slots"]["sede"]
                        print("🔄 Sede poblada en slots, continuando con siguiente paso del flujo...")
                    else:
                        print("🔄 Solo ciudad extraída, continuando con flujo normal para mostrar sedes...")
                    
                    # Limpiar el input_original_menu para evitar bucles
                    session_attributes.pop("input_original_menu", None)

                
                if resultado and resultado.get("consulta_directa") == True:
                    print("🎯 CONSULTA DIRECTA DETECTADA - Procesando inmediatamente")
                    
                    # Actualizar session_attributes con los datos del resultado
                    session_attributes.update(resultado.get("session_attributes", {}))
                    
                    # Actualizar slots del intent con los datos extraídos
                    if resultado.get("ciudad_id"):
                        intent["slots"]["ciudad"] = {
                            "value": {"interpretedValue": resultado["ciudad_nombre"]},
                            "shape": "Scalar"
                        }
                        
                    if resultado.get("sede_id"):
                        intent["slots"]["sede"] = {
                            "value": {"interpretedValue": resultado["sede_nombre"]},
                            "shape": "Scalar"
                        }
                        
                    if resultado.get("clase_id"):
                        intent["slots"]["clase"] = {
                            "value": {"interpretedValue": resultado["clase_nombre"]},
                            "shape": "Scalar"
                        }
                        
                    # 🔄 SI SOLO FALTA LA FECHA, PREGUNTARLA DIRECTAMENTE
                    if not resultado.get("fecha"):
                        print("🔍 Consulta directa detectada pero falta fecha - preguntando")
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
                                "intent": {
                                    "name": intent["name"],
                                    "slots": intent["slots"],
                                    "state": "InProgress"
                                },
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": f"¡Perfecto! Consulto los horarios de {resultado['clase_nombre']} en la sede {resultado['sede_nombre']}. 🏃‍♂️\n\n¿Para qué fecha deseas consultar? Puedes escribir:\n• YYYY-MM-DD (2025-01-15)\n• DD de MMMM (15 de enero)\n• DD/MM (15/01)\n• 'hoy' o 'mañana'"
                            }]
                        }
                    
                    # 🔄 SI TENEMOS TODOS LOS DATOS, EJECUTAR LA CONSULTA INMEDIATAMENTE
                    if resultado.get("tipo_consulta") == "2":
                        # Consulta tipo 2: Horarios de una clase específica
                        print(f"🎯 Ejecutando consulta tipo 2: clase {resultado['clase_nombre']} en sede {resultado['sede_nombre']} para {resultado['fecha']}")
                        
                        fecha_normalizada, error_fecha = normalizar_fecha(resultado["fecha"])
                        if error_fecha:
                            session_attributes.pop("preguntando_tipo_consulta_grupales", None)
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"{error_fecha}\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                        
                        # Ejecutar la consulta para clase específica
                        horarios = consultar_horarios_por_sede_clase_fecha(resultado["sede_id"], resultado["clase_id"], fecha_normalizada)
                        if horarios:
                            resumen = "\n".join(
                                f"- {h['hora_inicio']} a {h['hora_fin']}" for h in horarios
                            )
                            prompt = get_prompt_por_intent("ConsultaGrupales", f"Sede: {resultado['sede_nombre']}\nClase: {resultado['clase_nombre']}\nFecha: {fecha_normalizada}\n{resumen}")
                            mensaje_final = consultar_bedrock_generacion(prompt)
                            session_attributes["ultimo_intent_completado"] = "ConsultaGrupales"
                            session_attributes.pop("clase_display", None)
                            session_attributes.pop("slots_previos", None)
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"{mensaje_final or prompt}\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                        else:
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"No hay horarios para {resultado['clase_nombre']} en la sede {resultado['sede_nombre']} el {fecha_normalizada}.\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                
                if "opcion_menu" in resultado:
                    opcion = resultado["opcion_menu"]
                    print("opcion_menu detectada:", opcion)
                    if opcion == "1":
                        tipo_consulta = get_slot_value(intent["slots"], "tipo_consulta_grupales") or "1"
                        print("==> Unificando flujo opción 1 con tipo_consulta_grupales == '1'")
                        # Simula el flujo robusto de tipo_consulta == "1"
                        if tipo_consulta and tipo_consulta.strip() == "1":
                            print("=== DEBUG ConsultaGrupales Opción 1 (unificado)===")
                            print("session_attributes antes:", session_attributes)
                            print("intent['slots'] antes:", intent["slots"])
                            fecha = get_slot_value(intent["slots"], "fecha") or session_attributes.get("fecha")
                            ciudad_id = session_attributes.get("ciudad_id")
                            print("ciudad_id inicial:", ciudad_id)
                            # El usuario quiere ver todas las clases de una fecha
                            if not ciudad_id:
                                ciudad_slot = intent["slots"].get("ciudad", {})
                                ciudad_nombre = (
                                    ciudad_slot.get("value", {}).get("interpretedValue")
                                    if isinstance(ciudad_slot, dict) else None
                                ) or session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
                                print("ciudad_nombre para reconstrucción:", ciudad_nombre)
                                if ciudad_nombre:
                                    ciudad_id_tmp, ciudad_nombre_tmp, _, _ = validar_ciudad_usuario(
                                        {"ciudad": {"value": {"interpretedValue": ciudad_nombre}}},
                                        session_attributes,
                                        ciudad_nombre,
                                        intent
                                    )
                                    print("Resultado validar_ciudad_usuario:", ciudad_id_tmp, ciudad_nombre_tmp)
                                    if ciudad_id_tmp:
                                        ciudad_id = ciudad_id_tmp
                                        session_attributes["ciudad_id"] = str(ciudad_id)
                                        session_attributes["ciudad_nombre"] = ciudad_nombre_tmp
                            print("ciudad_id final:", ciudad_id)
                            print("session_attributes después:", session_attributes)
                            if not fecha:
                                print("DEBUG: Entrando a elicitar fecha porque no está presente")
                                return {
                                    "sessionState": {
                                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
                                        "intent": {
                                            "name": intent["name"],
                                            "slots": intent["slots"],
                                            "state": "InProgress"
                                        },
                                        "sessionAttributes": session_attributes
                                    },
                                    "messages": [{
                                        "contentType": "PlainText",
                                        "content": "¿Para qué fecha deseas consultar los horarios de la sede para clases? Puedes escribir:\n• YYYY-MM-DD (2025-07-04)\n• DD de MMMM (25 de enero)\n• DD/MM (25/01)\n• 'hoy' o 'mañana'"
                                    }]
                                }
                            else:
                                print("DEBUG: Ya tengo fecha, debería consultar clases")
                                fecha_normalizada, error_fecha = normalizar_fecha(fecha)
                                if error_fecha:
                                    session_attributes.pop("preguntando_tipo_consulta_grupales", None)
                                    session_attributes["esperando_transicion_grupales"] = "true"
                                    contenido = (
                                        f"{error_fecha}\n\n"
                                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n"
                                        "3️⃣ Otra clase\n"
                                        "4️⃣ Otra fecha\n"
                                        "🏠 M Menú principal\n\n"
                                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                                    )
                                    return {
                                        "sessionState": {
                                            "dialogAction": {"type": "ElicitIntent"},
                                            "intent": {"name": intent_name, "state": "Fulfilled"},
                                            "sessionAttributes": session_attributes
                                        },
                                        "messages": [{
                                            "contentType": "PlainText",
                                            "content": contenido
                                        }]
                                    }
                                fecha = fecha_normalizada
                                session_attributes.pop("preguntando_tipo_consulta_grupales", None)
                                id_sede = obtener_id_sede(get_slot_value(intent["slots"], "sede"))
                                if not id_sede:
                                    session_attributes["esperando_transicion_grupales"] = "true"
                                    contenido = (
                                        "No se pudo identificar la sede para consultar las clases.\n\n"
                                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n"
                                        "3️⃣ Otra clase\n"
                                        "4️⃣ Otra fecha\n"
                                        "🏠 M Menú principal\n\n"
                                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                                    )
                                    return {
                                        "sessionState": {
                                            "dialogAction": {"type": "ElicitIntent"},
                                            "intent": {"name": intent_name, "state": "Fulfilled"},
                                            "sessionAttributes": session_attributes
                                        },
                                        "messages": [{
                                            "contentType": "PlainText",
                                            "content": contenido
                                        }]
                                    }
                                clases_en_fecha = consultar_clases_grupales_por_sede_fecha(id_sede, fecha)
                                if clases_en_fecha:
                                    resumen = "\n".join(
                                        f"- {c['activity']} ({c['hora_inicio']} a {c['hora_fin']})" for c in clases_en_fecha
                                    )
                                    prompt = get_prompt_por_intent("ConsultaGrupales", f"Sede: {get_slot_value(intent['slots'], 'sede').title()}\nFecha: {fecha}\n{resumen}")
                                    mensaje_final = consultar_bedrock_generacion(prompt)
                                    session_attributes["ultimo_intent_completado"] = "ConsultaGrupales"
                                    session_attributes.pop("clase_display", None)
                                    session_attributes.pop("slots_previos", None)
                                    session_attributes["esperando_transicion_grupales"] = "true"
                                    contenido = (
                                        f"{mensaje_final or prompt}\n\n"
                                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n"
                                        "3️⃣ Otra clase\n"
                                        "4️⃣ Otra fecha\n"
                                        "🏠 M Menú principal\n\n"
                                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                                    )
                                    return {
                                        "sessionState": {
                                            "dialogAction": {"type": "ElicitIntent"},
                                            "intent": {"name": intent_name, "state": "Fulfilled"},
                                            "sessionAttributes": session_attributes
                                        },
                                        "messages": [{
                                            "contentType": "PlainText",
                                            "content": contenido
                                        }]
                                    }
                                else:
                                    session_attributes["esperando_transicion_grupales"] = "true"
                                    contenido = (
                                        f"No hay clases grupales para la sede {get_slot_value(intent['slots'], 'sede')} el {fecha}.\n\n"
                                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n"
                                        "3️⃣ Otra clase\n"
                                        "4️⃣ Otra fecha\n"
                                        "🏠 M Menú principal\n\n"
                                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                                    )
                                    return {
                                        "sessionState": {
                                            "dialogAction": {"type": "ElicitIntent"},
                                            "intent": {"name": intent_name, "state": "Fulfilled"},
                                            "sessionAttributes": session_attributes
                                        },
                                        "messages": [{
                                            "contentType": "PlainText",
                                            "content": contenido
                                        }]
                                    }
                
                    elif opcion == "2":
                        print("==> Entrando a opción 2 (clase específica)")
                        # El usuario quiere consultar una clase específica
                        sede_raw = get_slot_value(intent["slots"], "sede")
                        id_sede = obtener_id_sede(sede_raw)
                        clases = consultar_clases_por_sede_id(id_sede)
                        clases_nombres = [c['clase'] for c in clases] if clases and isinstance(clases[0], dict) else clases
                        if not clases_nombres:
                            return responder(f"No se encontraron clases para la sede {sede_raw}.", session_attributes, intent["name"])
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
                                    + "\n\n💬 ¿Cuál clase deseas consultar?"
                            }]
                        }
                if "sessionState" in resultado:
                    # Si el resultado es un objeto de respuesta completo, retornarlo directamente
                    return resultado
                
                # ✅ NUEVA LÓGICA: Manejar consulta directa cuando ya están todos los datos
                if resultado.get("consulta_directa") == True:
                    print("🎯 CONSULTA DIRECTA DETECTADA - Procesando inmediatamente")
                    
                    # Actualizar session_attributes con los datos del resultado
                    session_attributes.update(resultado.get("session_attributes", {}))
                    
                    if resultado.get("tipo_consulta") == "1":
                        # Consulta tipo 1: Todas las clases para una fecha
                        print(f"🎯 Ejecutando consulta tipo 1: todas las clases en sede {resultado['sede_nombre']} para {resultado['fecha']}")
                        
                        fecha_normalizada, error_fecha = normalizar_fecha(resultado["fecha"])
                        if error_fecha:
                            session_attributes.pop("preguntando_tipo_consulta_grupales", None)
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"{error_fecha}\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                        
                        # Ejecutar la consulta
                        clases_en_fecha = consultar_clases_grupales_por_sede_fecha(resultado["sede_id"], fecha_normalizada)
                        if clases_en_fecha:
                            resumen = "\n".join(
                                f"- {c['activity']} ({c['hora_inicio']} a {c['hora_fin']})" for c in clases_en_fecha
                            )
                            prompt = get_prompt_por_intent("ConsultaGrupales", f"Sede: {resultado['sede_nombre']}\nFecha: {fecha_normalizada}\n{resumen}")
                            mensaje_final = consultar_bedrock_generacion(prompt)
                            session_attributes["ultimo_intent_completado"] = "ConsultaGrupales"
                            session_attributes.pop("clase_display", None)
                            session_attributes.pop("slots_previos", None)
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"{mensaje_final or prompt}\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                        else:
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"No hay clases grupales para la sede {resultado['sede_nombre']} el {fecha_normalizada}.\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                    
                    elif resultado.get("tipo_consulta") == "2":
                        # Consulta tipo 2: Horarios de una clase específica
                        print(f"🎯 Ejecutando consulta tipo 2: clase {resultado['clase_nombre']} en sede {resultado['sede_nombre']} para {resultado['fecha']}")
                        
                        fecha_normalizada, error_fecha = normalizar_fecha(resultado["fecha"])
                        if error_fecha:
                            session_attributes.pop("preguntando_tipo_consulta_grupales", None)
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"{error_fecha}\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                        
                        # Ejecutar la consulta para clase específica
                        horarios = consultar_horarios_por_sede_clase_fecha(resultado["sede_id"], resultado["clase_id"], fecha_normalizada)
                        if horarios:
                            resumen = "\n".join(
                                f"- {h['hora_inicio']} a {h['hora_fin']}" for h in horarios
                            )
                            prompt = get_prompt_por_intent("ConsultaGrupales", f"Sede: {resultado['sede_nombre']}\nClase: {resultado['clase_nombre']}\nFecha: {fecha_normalizada}\n{resumen}")
                            mensaje_final = consultar_bedrock_generacion(prompt)
                            session_attributes["ultimo_intent_completado"] = "ConsultaGrupales"
                            session_attributes.pop("clase_display", None)
                            session_attributes.pop("slots_previos", None)
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"{mensaje_final or prompt}\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                        else:
                            session_attributes["esperando_transicion_grupales"] = "true"
                            contenido = (
                                f"No hay horarios para {resultado['clase_nombre']} en la sede {resultado['sede_nombre']} el {fecha_normalizada}.\n\n"
                                "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
                            )
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "intent": {"name": intent_name, "state": "Fulfilled"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": contenido
                                }]
                            }
                
                if "error" in resultado:
                    return resultado["error"]  # Devuelve la respuesta de error al usuario

                # Poblar los slots si todo es válido
                if resultado.get("ciudad_id"):
                    intent["slots"]["ciudad"] = {
                        "value": {"interpretedValue": resultado["ciudad_nombre"]},
                        "shape": "Scalar"
                    }
                    session_attributes["ciudad_id"] = str(resultado["ciudad_id"])
                    session_attributes["ciudad_nombre"] = resultado["ciudad_nombre"]

                if resultado.get("sede_id"):
                    intent["slots"]["sede"] = {
                        "value": {"interpretedValue": resultado["sede_nombre"]},
                        "shape": "Scalar"
                    }
                    session_attributes["sede_id"] = str(resultado["sede_id"])
                    session_attributes["sede_nombre"] = resultado["sede_nombre"]

                if resultado.get("clase_id"):
                    intent["slots"]["clase"] = {
                        "value": {"interpretedValue": resultado["clase_nombre"]},
                        "shape": "Scalar"
                    }
                    session_attributes["clase_id"] = str(resultado["clase_id"])
                    session_attributes["clase_nombre"] = resultado["clase_nombre"]

                if resultado.get("fecha"):
                    intent["slots"]["fecha"] = {
                        "value": {"interpretedValue": resultado["fecha"]},
                        "shape": "Scalar"
                    }
                
                # ✅ DEBUG: Verificar slots después de poblar
                print(f"🔍 DEBUG SLOTS DESPUÉS DE POBLAR:")
                print(f"  - intent['slots']: {intent['slots']}")
                print(f"  - slots (local): {slots}")
                print(f"  - ciudad_raw que se usará: {get_slot_value(slots, 'ciudad')}")
                print(f"  - sede_raw que se usará: {get_slot_value(slots, 'sede')}")
                print(f"  - clase_raw que se usará: {get_slot_value(slots, 'clase')}")
                print(f"🔍 ==========================================")
                
                # ✅ CRUCIAL: Sincronizar slots locales con intent["slots"] después de poblar
                if intent.get("slots"):
                    for slot_name, slot_data in intent["slots"].items():
                        if slot_data and slot_data.get("value"):
                            slots[slot_name.lower()] = slot_data
                            print(f"🔄 Sincronizado slot '{slot_name}' en slots locales")
                
                # ✅ AHORA SÍ: Extraer valores de slots con datos actualizados
                ciudad_raw = get_slot_value(slots, "ciudad")
                clase_raw = get_slot_value(slots, "clase")
                fecha = get_slot_value(slots, "fecha")
                print(f"🔍 VALORES FINALES EXTRAÍDOS:")
                print(f"  - ciudad_raw: '{ciudad_raw}'")
                print(f"  - clase_raw: '{clase_raw}'")
                print(f"  - fecha: '{fecha}'")
                
                if session_attributes.get("flujo_otra_sede") == "true":
                    print("🔍 Continuando flujo de otra sede...")
                    session_attributes.pop("flujo_otra_sede", None)
                session_attributes["en_flujo_activo"] = intent_name
                
                #Manejar transiciones primero
                tipo_transicion = get_slot_value(slots, "tipo_transicion")
                
                # Detectar transición desde input_transcript 
                if session_attributes.get("esperando_transicion_grupales") == "true":
                    input_lower = input_transcript.lower().strip()
                    tipo_transicion_slot = get_slot_value(slots, "tipo_transicion")
                    tipo_transicion = None

                    #  Prioridad: slot > input
                    if tipo_transicion_slot:
                        if tipo_transicion_slot == "1":
                            tipo_transicion = "otra_ciudad"
                            print(f"🔍 Transición detectada por slot: OTRA CIUDAD (1)")
                        elif tipo_transicion_slot == "2":
                            tipo_transicion = "otra_sede"
                            print(f"🔍 Transición detectada por slot: OTRA SEDE (2)")
                        elif tipo_transicion_slot == "3":
                            tipo_transicion = "otra_clase"
                            print(f"🔍 Transición detectada por slot: OTRA CLASE (3)")
                        elif tipo_transicion_slot == "4":
                            tipo_transicion = "no"
                            print(f"🔍 Transición detectada por slot: NO (4)")
                    else:
                        if input_lower in ["m", "menu", "menú", "menu principal", "menú principal"]:
                            # ...tu lógica de menú principal...
                            acepto_politicas = session_attributes.get("acepto_politicas")
                            documento_attrs = {
                                "document_type_id": session_attributes.get("document_type_id"),
                                "document_type_raw": session_attributes.get("document_type_raw"),
                                "document_number": session_attributes.get("document_number"),
                                "intenciones_con_documento": session_attributes.get("intenciones_con_documento")
                            }
                            session_attributes.clear()
                            for k, v in documento_attrs.items():
                                if v is not None:
                                    session_attributes[k] = v
                            if acepto_politicas == "true":
                                session_attributes["acepto_politicas"] = "true"
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitIntent"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": (
                                        "Has regresado al menú principal. ¿En qué puedo ayudarte?\n\n"
                                        "Algunas opciones:\n"
                                        "📄 Preguntas frecuentes sobre Bodytech\n"
                                        "🏢 Consultar sedes y horarios\n"
                                        "🏃‍♂️ Clases grupales disponibles\n"
                                        "📅 Información de tu plan\n"
                                        "👥 Consultar invitados\n"
                                        "🏆 Información sobre referidos\n"
                                        "🧾 Consultar incapacidades\n"
                                        "🛍️ Información de ventas\n"
                                        "❄️ Consultar congelaciones\n"
                                        "¿Sobre qué tema te gustaría que te ayude?"
                                    )
                                }]
                            }
                        elif input_lower == "1":
                            tipo_transicion = "otra_ciudad"
                            print(f"🔍 Transición detectada por input: OTRA CIUDAD (1)")
                        elif input_lower == "2":
                            tipo_transicion = "otra_sede"
                            print(f"🔍 Transición detectada por input: OTRA SEDE (2)")
                        elif input_lower == "3":
                            tipo_transicion = "otra_clase"
                            print(f"🔍 Transición detectada por input: OTRA CLASE (3)")
                        elif input_lower == "4":
                            tipo_transicion = "otra_fecha"
                            print(f"🔍 Transición detectada por input: OTRA FECHA (4)")
                        elif input_lower == "5":
                            tipo_transicion = "no"
                            print(f"🔍 Transición detectada por input: NO (5)")
                        else:
                            # Cualquier otra cosa es inválida
                            print(f"🔍 Opción inválida para transición: '{input_lower}'") 
                            contenido = (
                                "🤔 No entendí tu respuesta. Por favor, selecciona una opción válida:\n\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ Otra clase\n"
                                "4️⃣ Otra fecha\n\n"
                                "🏠 M Menú principal\n\n"
                                "💬 **Responde solo con el número o la letra M para menú principal:**"
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
                        
                        # Obtener ciudad_id para la consulta
                        ciudad_id = session_attributes.get("ciudad_id")
                        if not ciudad_id:
                            # Si no tenemos ciudad_id, necesitamos validar la ciudad
                            ciudad_id, ciudad_nombre, session_attributes, respuesta_ciudad = validar_ciudad_usuario(
                                {"ciudad": {"value": {"interpretedValue": ciudad_actual}}},
                                session_attributes,
                                "",
                                intent
                            )
                            if not ciudad_id:
                                return respuesta_ciudad

                        # Limpiar solo sede y clase, mantener ciudad
                        keys_to_remove = ["sede_nombre", "sede_id", "clase_display", "slots_previos"]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)

                        # Configurar slots para nueva consulta manteniendo la ciudad
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

                        # Actualizar el intent con los nuevos slots
                        intent["slots"] = slots_nuevos
                        slots = slots_nuevos

                        # Marcar que estamos en flujo activo
                        session_attributes["en_flujo_activo"] = "ConsultaGrupales"

                        print(f"✅ Slots configurados para otra sede: {slots}")
                        print(f"✅ Ciudad mantenida: {ciudad_actual}")
                        print(f"✅ Ciudad ID: {ciudad_id}")

                        # MOSTRAR SEDES DISPONIBLES INMEDIATAMENTE
                        try:
                            print(f"🔍 ===== CONSULTANDO SEDES PARA OTRA SEDE =====")
                            print(f"🔍 ciudad_id: {ciudad_id}")
                            print(f"🔍 Llamando consultar_sedes_por_ciudad_id({ciudad_id}, 1)")
                            
                            sedes = consultar_sedes_por_ciudad_id(ciudad_id)
                            print(f"🔍 Sedes obtenidas: {sedes}")
                            
                            sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                            print(f"🔍 sedes_nombres: {sedes_nombres}")
                            
                            if not sedes_nombres:
                                print(f"❌ No se encontraron sedes para ciudad_id: {ciudad_id}")
                                return responder(f"No se encontraron sedes para la ciudad {ciudad_actual}.", 
                                                session_attributes, intent_name, fulfillment_state="Fulfilled")
                            
                            print(f"🔍 ===== CONSTRUYENDO RESPUESTA SEDES PARA OTRA SEDE =====")
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
                                    "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": f"📍 **Otras sedes disponibles en {ciudad_actual}:**\n\n"
                                        + "\n".join(f"- {sede}" for sede in sedes_nombres)
                                        + f"\n\n💬 ¿En cuál sede deseas consultar las clases grupales?\n\n"
                                        "🏠 M Menú principal\n"
                                        "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                                }]
                            }
                            
                        except Exception as e:
                            print(f"❌ ERROR en consulta de sedes para otra sede: {str(e)}")
                            import traceback
                            print(f"❌ Traceback: {traceback.format_exc()}")
                            
                            # Fallback en caso de error
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
                                    "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": f"¡Perfecto! Te ayudo a consultar otra sede en {ciudad_actual}. ¿En qué sede deseas consultar?"
                                }]
                            }
                    
                    elif tipo_transicion == "otra_ciudad":
                        print("✅ Transición: OTRA CIUDAD")
                        
                        # Limpiar toda la información geográfica
                        keys_to_remove = [
                            "clase_display", "slots_previos",
                            "sede_nombre", "sede_id", "ciudad_nombre", "ciudad_id", "ciudad"
                        ]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        # Empezar desde cero - NO configurar slots de ciudad
                        slots = {}
                        print("✅ Iniciando consulta en nueva ciudad - slots vacíos")

                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                                "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText", 
                                "content": "¡Perfecto! 🌎 ¿En qué ciudad deseas consultar las clases grupales?"
                            }]
                        }
                        
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
                        # Obtener id_sede para mostrar clases disponibles
                        sede_id = session_attributes.get("sede_id")
                        if not sede_id:
                            # Si no tenemos sede_id, calcularlo
                            sede_normalizada = normalizar_nombre(sede_actual)
                            sede_id = obtener_id_sede(sede_normalizada)
                            if not sede_id:
                                return {
                                    "sessionState": {
                                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
                                        "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                                        "sessionAttributes": session_attributes
                                    },
                                    "messages": [{"contentType": "PlainText", "content": f"No se pudo identificar la sede {sede_actual}. ¿En qué sede deseas consultar?"}]
                                }
                        
                        # Limpiar solo clase info, mantener ciudad y sede
                        keys_to_remove = ["clase_display", "slots_previos"]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        # Configurar slots manteniendo ciudad y sede
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
                        
                        # Marcar que estamos en flujo activo
                        session_attributes["en_flujo_activo"] = "ConsultaGrupales"
                        
                    elif tipo_transicion == "otra_sede":
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
                        keys_to_remove = ["sede_nombre", "sede_id", "clase_display", "slots_previos"]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        # Configurar slots para nueva consulta manteniendo la ciudad
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
                        
                        # Actualizar el intent con los nuevos slots
                        intent["slots"] = slots_nuevos
                        slots = slots_nuevos
                        
                        # Marcar que estamos en flujo activo
                        session_attributes["en_flujo_activo"] = "ConsultaGrupales"
                        
                        # Limpiar la bandera de transición
                        session_attributes.pop("esperando_transicion_grupales", None)
                        
                        print(f"✅ Slots configurados para otra sede: {slots}")
                        print(f"✅ Ciudad mantenida: {ciudad_actual}")
                        
                        # Continuar con el flujo normal - no retornar aquí, dejar que continúe
                        print("🔄 Continuando con flujo normal para mostrar sedes...")
                        
                    elif tipo_transicion == "otra_fecha":
                        print("✅ Transición: OTRA FECHA")
                        ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
                        sede_actual = session_attributes.get("sede_nombre")
                        clase_actual = session_attributes.get("clase_nombre")
                        
                        if not ciudad_actual or not sede_actual:
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                                    "intent": {"name": "ConsultaGrupales", "state": "InProgress", "slots": {}},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad y sede deseas consultar otra fecha?"}]
                            }
                        
                        # Limpiar solo datos de fecha, mantener todo lo demás
                        keys_to_remove = ["esperando_transicion_grupales"]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
                        
                        # Configurar slots manteniendo ciudad, sede y clase
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
                        
                        if clase_actual:
                            slots_nuevos["clase"] = {
                                "value": {"originalValue": clase_actual, "resolvedValues": [clase_actual], "interpretedValue": clase_actual},
                                "shape": "Scalar"
                            }
                        
                        # Actualizar el intent con los nuevos slots
                        intent["slots"] = slots_nuevos
                        slots = slots_nuevos
                        
                        # Marcar que estamos en flujo activo
                        session_attributes["en_flujo_activo"] = "ConsultaGrupales"
                        
                        print(f"✅ Parámetros mantenidos para nueva fecha - Ciudad: {ciudad_actual}, Sede: {sede_actual}, Clase: {clase_actual or 'Todas'}")
                        
                        # Preguntar por la nueva fecha directamente
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
                                "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": f"¡Perfecto! Te ayudo a consultar {'las clases' if not clase_actual else clase_actual} en la sede {sede_actual} ({ciudad_actual}) para otra fecha. 📅\n\n¿Para qué fecha deseas consultar? Puedes escribir:\n• YYYY-MM-DD (2025-01-15)\n• DD de MMMM (15 de enero)\n• DD/MM (15/01)\n• 'hoy' o 'mañana'"
                            }]
                        }

                    elif tipo_transicion == "no":
                        print("✅ Usuario no desea más consultas")
                        # Limpiar todo y enviar pregunta final
                        keys_to_remove = [
                            "en_flujo_activo", "clase_display", "slots_previos",
                            "ciudad_nombre", "sede_nombre", "ciudad_id", "sede_id", "esperando_transicion_grupales"
                        ]
                        for key in keys_to_remove:
                            session_attributes.pop(key, None)
    
                        # Validación centralizada SOLO aquí
                        respuesta_forzada = validar_y_forzar_flujo_ciudad(
                            intent_name, slots, session_attributes, input_transcript, intent, flujo_grupales_por_ciudad
                        )
                        if respuesta_forzada:
                            return respuesta_forzada

                        return responder_con_pregunta_final("¡Perfecto! 😊", session_attributes, "ConsultaGrupales")
                
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
                
                # ✅ MOVER DESPUÉS: Extraer valores de slots DESPUÉS de poblar con resultado
                # Se hace más abajo para asegurar que tengan los valores actualizados
                
                # después de extraer los slots y antes de consultar clases
                ciudad_id = session_attributes.get("ciudad_id")
                # sede_raw = get_slot_value(slots, "sede") # ✅ MOVIDO ARRIBA - Ya se extrae después de poblar slots
                sede_raw = get_slot_value(slots, "sede")  # ✅ MANTENER: Extracción final por si acaso
                if sede_raw:
                    id_sede = obtener_id_sede(sede_raw)
                    if not id_sede:
                        # Si no hay ciudad_id, primero pregunta por ciudad
                        if not ciudad_id:
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                                    "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": "No reconozco la sede ingresada y tampoco tengo la ciudad. ¿En qué ciudad deseas consultar las clases grupales?"
                                }]
                            }
                        # Si hay ciudad_id, muestra las sedes disponibles
                        sedes = consultar_sedes_por_ciudad_id(ciudad_id)
                        sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
                                "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    f"No hay ninguna sede con el nombre '{sede_raw}'. Las sedes disponibles en esta ciudad son:\n\n"
                                    + "\n".join(f"- {sede}" for sede in sedes_nombres)
                                    + f"\n\n💬 ¿En cuál sede deseas consultar las clases grupales?\n\n"
                                        "🏠 M Menú principal\n"
                                        "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                                )
                            }]
                        }

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
                        sedes = consultar_sedes_por_ciudad_id(ciudad_id)
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
                                    + f"\n\n💬 ¿En cuál sede deseas consultar las clases grupales?\n\n"
                                        "🏠 M Menú principal\n"
                                        "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
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
                    # Procesar la respuesta a la pregunta intermedia
                    tipo_consulta = get_slot_value(slots, "tipo_consulta_grupales")
                    if not tipo_consulta:
                        # Pregunta intermedia SIEMPRE si no hay tipo_consulta_grupales
                        session_attributes["preguntando_tipo_consulta_grupales"] = "true"
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_consulta_grupales"},
                                "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    f"¿Qué deseas consultar en la sede {sede_raw.title()}?\n\n"
                                    "1️⃣ Todas las clases grupales disponibles en una fecha específica\n"
                                    "2️⃣ Los horarios de una clase específica\n\n"
                                    "💬 **Responde solo con el número (1 o 2):**"
                                )
                            }]
                        }
                    if tipo_consulta:
                        print(f"DEBUG tipo_consulta_grupales: {tipo_consulta}")
                        print(f"DEBUG slot fecha antes de procesar: {fecha}")
                        print(f"DEBUG slots actuales: {json.dumps(intent['slots'], indent=2)}")
                        print("DEBUG antes de pop:", session_attributes)
                        session_attributes.pop("preguntando_tipo_consulta_grupales", None)
                        print("DEBUG después de pop:", session_attributes)
                        
                        intent["slots"]["tipo_consulta_grupales"] = {
                            "value": {
                                "originalValue": tipo_consulta,
                                "resolvedValues": [tipo_consulta],
                                "interpretedValue": tipo_consulta
                            },
                            "shape": "Scalar"
                        }
                        slots["tipo_consulta_grupales"] = intent["slots"]["tipo_consulta_grupales"]
                        # Guardar también en session_attributes para mantener el estado
                        session_attributes["tipo_consulta_grupales"] = tipo_consulta
                        tipo_consulta = get_slot_value(slots, "tipo_consulta_grupales")

                        if tipo_consulta and tipo_consulta.strip() == "1":
                            print("=== DEBUG ConsultaGrupales Opción 1 ===")
                            print("session_attributes antes:", session_attributes)
                            print("intent['slots'] antes:", intent["slots"])
                            fecha = get_slot_value(intent["slots"], "fecha") or session_attributes.get("fecha")
                            ciudad_id = session_attributes.get("ciudad_id")
                            print("ciudad_id inicial:", ciudad_id)
                            # El usuario quiere ver todas las clases de una fecha
                            if not ciudad_id:
                                ciudad_slot = intent["slots"].get("ciudad", {})
                                ciudad_nombre = (
                                    ciudad_slot.get("value", {}).get("interpretedValue")
                                    if isinstance(ciudad_slot, dict) else None
                                ) or session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
                                print("ciudad_nombre para reconstrucción:", ciudad_nombre)
                                if ciudad_nombre:
                                    ciudad_id_tmp, ciudad_nombre_tmp, _, _ = validar_ciudad_usuario(
                                        {"ciudad": {"value": {"interpretedValue": ciudad_nombre}}},
                                        session_attributes,
                                        ciudad_nombre,
                                        intent
                                    )
                                    print("Resultado validar_ciudad_usuario:", ciudad_id_tmp, ciudad_nombre_tmp)
                                    if ciudad_id_tmp:
                                        ciudad_id = ciudad_id_tmp
                                        session_attributes["ciudad_id"] = str(ciudad_id)
                                        session_attributes["ciudad_nombre"] = ciudad_nombre_tmp
                            print("ciudad_id final:", ciudad_id)
                            print("session_attributes después:", session_attributes)
                            if not fecha:
                                print("DEBUG: Entrando a elicitar fecha porque no está presente")
                                return {
                                    "sessionState": {
                                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
                                        "intent": {
                                            "name": intent["name"],
                                            "slots": intent["slots"],
                                            "state": "InProgress"
                                        },
                                        "sessionAttributes": session_attributes
                                    },
                                    "messages": [{
                                        "contentType": "PlainText",
                                        "content": "¿Para qué fecha deseas consultar los horarios de la sede para clases? Puedes escribir:\n• YYYY-MM-DD (2025-07-04)\n• DD de MMMM (25 de enero)\n• DD/MM (25/01)\n• 'hoy' o 'mañana'"
                                    }]
                                }
                            else:
                                print("DEBUG: Ya tengo fecha, debería consultar clases")
                                fecha_normalizada, error_fecha = normalizar_fecha(fecha)
                                if error_fecha:
                                    session_attributes.pop("preguntando_tipo_consulta_grupales", None)
                                    session_attributes["esperando_transicion_grupales"] = "true"
                                    contenido = (
                                        f"{error_fecha}\n\n"
                                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n"
                                        "3️⃣ Otra clase\n"
                                        "4️⃣ Otra fecha\n"
                                        "🏠 M Menú principal\n\n"
                                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                                    )
                                    return {
                                        "sessionState": {
                                            "dialogAction": {"type": "ElicitIntent"},
                                            "intent": {"name": intent_name, "state": "Fulfilled"},
                                            "sessionAttributes": session_attributes
                                        },
                                        "messages": [{
                                            "contentType": "PlainText",
                                            "content": contenido
                                        }]
                                    }
                                fecha = fecha_normalizada
                                session_attributes.pop("preguntando_tipo_consulta_grupales", None)
                                id_sede = obtener_id_sede(sede_raw)
                                if not id_sede:
                                    session_attributes["esperando_transicion_grupales"] = "true"
                                    contenido = (
                                        "No se pudo identificar la sede para consultar las clases.\n\n"
                                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n"
                                        "3️⃣ Otra clase\n"
                                        "4️⃣ Otra fecha\n"
                                        "🏠 M Menú principal\n\n"
                                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                                    )
                                    return {
                                        "sessionState": {
                                            "dialogAction": {"type": "ElicitIntent"},
                                            "intent": {"name": intent_name, "state": "Fulfilled"},
                                            "sessionAttributes": session_attributes
                                        },
                                        "messages": [{
                                            "contentType": "PlainText",
                                            "content": contenido
                                        }]
                                    }
                                clases_en_fecha = consultar_clases_grupales_por_sede_fecha(id_sede, fecha)
                                if clases_en_fecha:
                                    resumen = "\n".join(
                                        f"- {c['activity']} ({c['hora_inicio']} a {c['hora_fin']})" for c in clases_en_fecha
                                    )
                                    prompt = get_prompt_por_intent("ConsultaGrupales", f"Sede: {sede_raw.title()}\nFecha: {fecha}\n{resumen}")
                                    mensaje_final = consultar_bedrock_generacion(prompt)
                                    session_attributes["ultimo_intent_completado"] = "ConsultaGrupales"
                                    session_attributes.pop("clase_display", None)
                                    session_attributes.pop("slots_previos", None)
                                    session_attributes["esperando_transicion_grupales"] = "true"
                                    contenido = (
                                        f"{mensaje_final or prompt}\n\n"
                                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n" 
                                        "3️⃣ Otra clase\n"
                                        "4️⃣ Otra fecha\n"
                                        "🏠 M Menú principal\n\n"
                                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                                    )
                                    return {
                                        "sessionState": {
                                            "dialogAction": {"type": "ElicitIntent"},
                                            "intent": {"name": intent_name, "state": "Fulfilled"},
                                            "sessionAttributes": session_attributes
                                        },
                                        "messages": [{
                                            "contentType": "PlainText",
                                            "content": contenido
                                        }]
                                    }
                                else:
                                    session_attributes["esperando_transicion_grupales"] = "true"
                                    contenido = (
                                        f"No hay clases grupales para la sede {sede_raw} el {fecha}.\n\n"
                                        "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                                        "Selecciona una opción:\n"
                                        "1️⃣ Otra ciudad\n"
                                        "2️⃣ Otra sede\n"
                                        "3️⃣ Otra clase\n"
                                        "4️⃣ Otra fecha\n"
                                        "🏠 M Menú principal\n\n"
                                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                                    )
                                    return {
                                        "sessionState": {
                                            "dialogAction": {"type": "ElicitIntent"},
                                            "intent": {"name": intent_name, "state": "Fulfilled"},
                                            "sessionAttributes": session_attributes
                                        },
                                        "messages": [{
                                            "contentType": "PlainText",
                                            "content": contenido
                                        }]
                                    }
                        elif tipo_consulta.strip() == "2":
                            # El usuario quiere consultar una clase específica: mostrar listado de clases
                            id_sede = obtener_id_sede(sede_raw)
                            clases = consultar_clases_por_sede_id(id_sede)
                            clases_nombres = [c['clase'] for c in clases] if clases and isinstance(clases[0], dict) else clases
                            if not clases_nombres:
                                return responder(f"No se encontraron clases para la sede {sede_raw}.", session_attributes, intent_name)
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
                                        + "\n\n💬 ¿Cuál clase deseas consultar?"
                                }]
                            }
                        else:
                            # Respuesta inválida
                            session_attributes["preguntando_tipo_consulta_grupales"] = "true"
                            return {
                                "sessionState": {
                                    "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_consulta_grupales"},
                                    "intent": {"name": intent["name"], "slots": intent["slots"], "state": "InProgress"},
                                    "sessionAttributes": session_attributes
                                },
                                "messages": [{
                                    "contentType": "PlainText",
                                    "content": (
                                        "Por favor responde solo con el número:\n"
                                        "1️⃣ Todas las clases grupales de una fecha\n"
                                        "2️⃣ Horarios de una clase específica"
                                    )
                                }]
                            }

                # 5. VALIDAR CLASE
                if clase_raw:
                    id_sede = obtener_id_sede(sede_raw)
                    if not id_sede:
                        return responder("No se pudo identificar la sede para validar las clases.", 
                                       session_attributes, intent_name, fulfillment_state="Fulfilled")
                    
                    print(f"🔍 Validando clase: '{clase_raw}' en sede_id: {id_sede}")
                    clase_id, clase_nombre, session_attributes, respuesta_clase = validar_clase_usuario(
                        slots, session_attributes, input_transcript, intent, id_sede
                    )
                    if respuesta_clase:
                        return respuesta_clase
                    if clase_nombre:
                        clase_raw = clase_nombre.lower()
                        print(f"🔍 Clase corregida a: '{clase_raw}'")
                        session_attributes["clase_display"] = clase_nombre
                        slots["clase"] = intent["slots"]["clase"]
                        print(f"✅ Slots actualizados después de corrección de clase")
                else:
                    # 4. ELICITAR CLASE si sede está presente
                    id_sede = obtener_id_sede(sede_raw)
                    
                    clases = consultar_clases_por_sede_id(id_sede)
                    clases_nombres = [c['activity'] for c in clases] if clases and isinstance(clases[0], dict) else clases
                    
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

                tipo_consulta = get_slot_value(slots, "tipo_consulta_grupales")

                # 8. CONSULTAR HORARIOS (solo si es opción 2)
                if tipo_consulta and tipo_consulta.strip() == "2":
                    if not all([ciudad_raw, sede_raw, clase_raw, fecha]):
                        return responder(
                            "Faltan datos para consultar las clases grupales. Por favor, asegúrate de indicar ciudad, sede, clase y fecha.",
                            session_attributes, intent_name, fulfillment_state="Fulfilled"
                        )
                
                from services import obtener_id_actividad
                id_sede = obtener_id_sede(sede_raw)
                id_clase = obtener_id_actividad(clase_raw)
                
                if not id_sede or not id_clase:
                    return responder("No se encontró la sede o clase indicada. Por favor, revisa los nombres.",
                                   session_attributes, intent_name, fulfillment_state="Fulfilled")

                horarios = consultar_horarios_por_sede_clase_fecha(id_sede, id_clase, fecha)
                if not horarios:
                    # No hay horarios disponibles - preguntar si desea otra consulta
                    mensaje_sin_horarios = f"No hay horarios disponibles para {clase_raw} en la sede {sede_raw} el {fecha}."
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
                        "4️⃣ Otra fecha\n"
                        "🏠 M Menú principal\n\n"
                        "💬 **Responde solo con el número o la letra M para menú principal:**"
                    )
                    
                    return {
                        "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "intent": {
                            "name": intent_name,
                            "state": "Fulfilled"
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
                
                # Limpiar datos de la consulta actual
                session_attributes.pop("clase_display", None)
                session_attributes.pop("slots_previos", None)
                
                # Configurar para esperar transición específica de ConsultaGrupales
                session_attributes["esperando_transicion_grupales"] = "true"
                
                contenido = (
                    f"{mensaje_final}\n\n"
                    "¿Deseas hacer otra consulta de clases? 🏃‍♂️\n\n"
                    "Selecciona una opción:\n"
                    "1️⃣ Otra ciudad\n"
                    "2️⃣ Otra sede\n"
                    "3️⃣ Otra clase\n"
                    "4️⃣ Otra fecha\n"
                    "🏠 M Menú principal\n\n"
                    "💬 **Responde solo con el número o la letra M para menú principal:**"
                )
                
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "intent": {
                            "name": intent_name,
                            "state": "Fulfilled"
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
            # ✅ RESETEAR CONTADOR AL INICIAR FLUJO EXITOSO
            from utils import resetear_contador_no_reconocidas
            resetear_contador_no_reconocidas(session_attributes)
            
            session_attributes["en_flujo_activo"] = intent_name
            
            # 🆕 VERIFICAR SI VIENE DEL PROCESAMIENTO AUTOMÁTICO
            if session_attributes.get("procesamiento_automatico_sedes") == "true":
                print("🎯 Procesamiento automático activado - usando datos extraídos")
                
                # Obtener datos extraídos
                datos_extraidos = session_attributes.get("datos_extraidos_sedes", {})
                input_para_procesar = session_attributes.get("input_original_menu", input_transcript)
                
                # Limpiar flags temporales
                session_attributes.pop("procesamiento_automatico_sedes", None)
                session_attributes.pop("input_original_menu", None)
                session_attributes.pop("datos_extraidos_sedes", None)
                
                # Procesar según tipo de consulta
                if datos_extraidos.get("tipo_consulta") == "categoria_especifica":
                    print(f"🎯 Ejecutando consulta de categoría: {datos_extraidos['categoria_nombre']} en {datos_extraidos['ciudad_nombre']}")
                    
                    brand_id = 1  # Solo Bodytech
                    categoria_valida = datos_extraidos["categoria_nombre"]
                    ciudad_id = datos_extraidos["ciudad_id"]
                    ciudad_nombre = datos_extraidos["ciudad_nombre"]
                    
                    id_categoria = obtener_id_categoria_por_nombre(categoria_valida, brand_id)
                    if not id_categoria:
                        return responder(
                            "No se encontró la categoría seleccionada.",
                            session_attributes,
                            intent_name,
                            fulfillment_state="Fulfilled"
                        )
                    
                    sedes = consultar_sedes_por_ciudad_id_linea(brand_id, id_categoria, ciudad_id)
                    sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                    
                    mensaje = (
                        f"🏢 **Sedes de {categoria_valida.title()} en {ciudad_nombre.title()}:**\n\n"
                        + "\n".join(f"• {s}" for s in sedes_nombres)
                        if sedes_nombres else f"❌ No se encontraron sedes de {categoria_valida} en {ciudad_nombre}."
                    )
                    
                    # Preguntar si desea consultar horarios
                    session_attributes["consultando_horarios"] = "preguntando"
                    mensaje_completo = f"{mensaje}\n\n¿Deseas consultar los horarios de alguna sede específica? 🕐\n\n"
                    mensaje_completo += "Si deseas consultar los horarios, escribe el nombre de la sede directamente.\n"
                    mensaje_completo += "Recuerda que puedes volver al Menú principal escribiendo 🏠\"M\".\n\n"
                    mensaje_completo += "💬 *Ejemplo:* 'Chapinero' o 'M'"

                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "Sede"},
                            "intent": {"name": intent_name, "slots": intent["slots"], "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{"contentType": "PlainText", "content": mensaje_completo}]
                    }
                    
                elif datos_extraidos.get("tipo_consulta") == "mostrar_categorias":
                    print(f"🎯 Mostrando categorías en {datos_extraidos['ciudad_nombre']}")
                    
                    categorias = obtener_categorias_por_linea("bodytech")
                    categorias_texto = "\n".join(f"   • {cat}" for cat in categorias)
                    
                    session_attributes["pregunta_categoria"] = "pendiente"
                    
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                            "intent": {
                                "name": intent_name,
                                "slots": intent["slots"],
                                "state": "InProgress"
                            },
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                f"📋 **Categorías de sedes disponibles en {datos_extraidos['ciudad_nombre'].title()}:**\n\n"
                                f"{categorias_texto}\n\n"
                                "¿Cómo te gustaría ver las sedes?\n\n"
                                "🎯 **'Por categoría'** - Te muestro las sedes organizadas por tipo\n"
                                "📋 **'Todas'** - Te muestro todas las sedes de una vez\n\n"
                                "💬 **Responde:** 'Por categoría' o 'Todas'"
                            )
                        }]
                    }
                    
                elif datos_extraidos.get("tipo_consulta") == "sede_especifica":
                    print(f"🎯 Consultando sede específica: {datos_extraidos['sede_nombre']}")
                    
                    sede_id = datos_extraidos["sede_id"]
                    sede_nombre = datos_extraidos["sede_nombre"]
                    
                    horarios = consultar_horarios_sede(sede_id)
                    if not horarios:
                        mensaje_final = f"No se encontraron horarios para la sede {sede_nombre.title()}."
                    else:
                        mensaje_final = respuesta_bedrock("ConsultarSedes", horarios)
                        if not mensaje_final or not mensaje_final.strip():
                            mensaje_final = f"Horarios de la sede {sede_nombre.title()} consultados exitosamente."
                    
                    # Preguntar por más consultas
                    session_attributes["esperando_transicion_sedes"] = "true"
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                            "intent": {"name": intent_name, "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                f"{mensaje_final}\n\n"
                                "¿Deseas hacer otra consulta de sedes? 🏢\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ No gracias\n\n"
                                "🏠 M Menú principal\n"
                                "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                            )
                        }]
                    }
            
            # 🔄 FLUJO NORMAL O MEJORADO
            elif session_attributes.get("mejoramiento_sedes") == "true":
                print("🎯 Mejoramiento de sedes activado - usando datos detectados")
                
                input_para_procesar = session_attributes.get("input_original_menu", input_transcript)
                session_attributes.pop("mejoramiento_sedes", None)
                session_attributes.pop("input_original_menu", None)
                
                # Poblar slots con los datos detectados si existen
                if session_attributes.get("ciudad_id"):
                    intent["slots"]["ciudad"] = {
                        "value": {"interpretedValue": session_attributes["ciudad_nombre"]},
                        "shape": "Scalar"
                    }
                    slots["ciudad"] = intent["slots"]["ciudad"]
                
                # Continuar con flujo normal (no hacer return aquí)
            
            # 🔄 CONTINUAR CON EL FLUJO NORMAL (resto del código existente)
            # Procesar detección automática al inicio
            try:
                print(f"🔍 Analizando input para detección automática de sedes: '{input_transcript}'")
                
                # Usar la función de extracción automática
                resultado_sedes = extraer_y_validar_slots_sedes(input_transcript, session_attributes, intent)
                
                print(f"🔍 Resultado extraer_y_validar_slots_sedes: {resultado_sedes}")
                
                # CASO 1: Si la función devuelve una respuesta directa (pregunta), enviarla
                if (resultado_sedes and resultado_sedes.get("sessionState")):
                    print("✅ Función devolvió una pregunta - enviando respuesta directa")
                    return resultado_sedes
                
                # CASO 2: Si es consulta directa, procesar inmediatamente
                if resultado_sedes.get("consulta_directa") == True:
                    print("🎯 CONSULTA DIRECTA DETECTADA - Procesando inmediatamente")
                    
                    # Actualizar session_attributes con los datos del resultado
                    session_attributes.update(resultado_sedes.get("session_attributes", {}))
                    
                    if resultado_sedes.get("tipo_consulta") == "categoria_especifica":
                        # Consulta de categoría específica
                        print(f"🎯 Ejecutando consulta de categoría: {resultado_sedes['categoria_nombre']} en {resultado_sedes['ciudad_nombre']}")
                        
                        brand_id = 1  # Solo Bodytech
                        categoria_valida = resultado_sedes["categoria_nombre"]
                        ciudad_id = resultado_sedes["ciudad_id"]
                        ciudad_nombre = resultado_sedes["ciudad_nombre"]
                        
                        id_categoria = obtener_id_categoria_por_nombre(categoria_valida, brand_id)
                        if not id_categoria:
                            return responder(
                                "No se encontró la categoría seleccionada.",
                                session_attributes,
                                intent_name,
                                fulfillment_state="Fulfilled"
                            )
                        
                        sedes = consultar_sedes_por_ciudad_id_linea(brand_id, id_categoria, ciudad_id)
                        sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                        
                        mensaje = (
                            f"🏢 **Sedes de {categoria_valida.title()} en {ciudad_nombre.title()}:**\n\n"
                            + "\n".join(f"• {s}" for s in sedes_nombres)
                            if sedes_nombres else f"❌ No se encontraron sedes de {categoria_valida} en {ciudad_nombre}."
                        )
                        
                        # Preguntar si desea consultar horarios
                        session_attributes["consultando_horarios"] = "preguntando"
                        mensaje_completo = f"{mensaje}\n\n¿Deseas consultar los horarios de alguna sede específica? 🕐\n\n"
                        mensaje_completo += "Si deseas consultar los horarios, escribe el nombre de la sede directamente.\n"
                        mensaje_completo += "Recuerda que puedes volver al Menú principal escribiendo 🏠\"M\".\n\n"
                        mensaje_completo += "💬 *Ejemplo:* 'Chapinero' o 'M'"

                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "Sede"},
                                "intent": {"name": intent_name, "slots": intent["slots"], "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{"contentType": "PlainText", "content": mensaje_completo}]
                        }
                        
                    elif resultado_sedes.get("tipo_consulta") == "mostrar_categorias":
                        # Mostrar categorías disponibles
                        print(f"🎯 Mostrando categorías en {resultado_sedes['ciudad_nombre']}")
                        
                        categorias = obtener_categorias_por_linea("bodytech")
                        categorias_texto = "\n".join(f"   • {cat}" for cat in categorias)
                        
                        session_attributes["pregunta_categoria"] = "pendiente"
                        
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                                "intent": {
                                    "name": intent_name,
                                    "slots": intent["slots"],
                                    "state": "InProgress"
                                },
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    f"📋 **Categorías de sedes disponibles en {resultado_sedes['ciudad_nombre'].title()}:**\n\n"
                                    f"{categorias_texto}\n\n"
                                    "¿Cómo te gustaría ver las sedes?\n\n"
                                    "🎯 **'Por categoría'** - Te muestro las sedes organizadas por tipo\n"
                                    "📋 **'Todas'** - Te muestro todas las sedes de una vez\n\n"
                                    "💬 **Responde:** 'Por categoría' o 'Todas'"
                                )
                            }]
                        }
                        
                    elif resultado_sedes.get("tipo_consulta") == "sede_especifica":
                        # Consulta de sede específica - mostrar horarios directamente
                        print(f"🎯 Consultando sede específica: {resultado_sedes['sede_nombre']}")
                        
                        sede_id = resultado_sedes["sede_id"]
                        sede_nombre = resultado_sedes["sede_nombre"]
                        
                        horarios = consultar_horarios_sede(sede_id)
                        if not horarios:
                            mensaje_final = f"No se encontraron horarios para la sede {sede_nombre.title()}."
                        else:
                            mensaje_final = respuesta_bedrock("ConsultarSedes", horarios)
                            if not mensaje_final or not mensaje_final.strip():
                                mensaje_final = f"Horarios de la sede {sede_nombre.title()} consultados exitosamente."
                        
                        # Preguntar por más consultas
                        session_attributes["esperando_transicion_sedes"] = "true"
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                                "intent": {"name": intent_name, "state": "InProgress", "slots": {}},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    f"{mensaje_final}\n\n"
                                    "¿Deseas hacer otra consulta de sedes? 🏢\n\n"
                                    "Selecciona una opción:\n"
                                    "1️⃣ Otra ciudad\n"
                                    "2️⃣ Otra sede\n"
                                    "3️⃣ No gracias\n\n"
                                    "🏠 M Menú principal\n"
                                    "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                                )
                            }]
                        }
                        
                    elif resultado_sedes.get("tipo_consulta") == "horarios_sede":
                        # 🆕 NUEVA FUNCIONALIDAD: Consulta directa de horarios de sede
                        print(f"🎯 CONSULTA DIRECTA DE HORARIOS: {resultado_sedes['sede_nombre']} en {resultado_sedes['ciudad_nombre']}")
                        
                        sede_id = resultado_sedes["sede_id"]
                        sede_nombre = resultado_sedes["sede_nombre"]
                        ciudad_nombre = resultado_sedes["ciudad_nombre"]
                        
                        horarios = consultar_horarios_sede(sede_id)
                        if not horarios:
                            mensaje_final = f"No se encontraron horarios para la sede {sede_nombre} en {ciudad_nombre}. 🕐"
                        else:
                            # Usar el prompt específico para horarios de sede
                            mensaje_final = respuesta_bedrock("ConsultarSedes", horarios)
                            if not mensaje_final or not mensaje_final.strip():
                                mensaje_final = f"Horarios de atención de la sede {sede_nombre} en {ciudad_nombre}:\n\n📅 Consulta completada exitosamente."
                        
                        # Preguntar por más consultas
                        session_attributes["esperando_transicion_sedes"] = "true"
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                                "intent": {"name": intent_name, "state": "InProgress", "slots": {}},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    f"{mensaje_final}\n\n"
                                    "¿Deseas hacer otra consulta de sedes? 🏢\n\n"
                                    "Selecciona una opción:\n"
                                    "1️⃣ Otra ciudad\n"
                                    "2️⃣ Otra sede\n"
                                    "3️⃣ No gracias\n\n"
                                    "🏠 M Menú principal\n"
                                    "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                                )
                            }]
                        }
                
                # CASO 3: Si solo se detectó ciudad, continuar con flujo normal
                if resultado_sedes.get("ciudad_id"):
                    print("✅ Ciudad detectada, continuando con flujo normal")
                    # Poblar slots y session_attributes
                    session_attributes.update(resultado_sedes.get("session_attributes", {}))
                    
                    intent["slots"]["ciudad"] = {
                        "value": {"interpretedValue": resultado_sedes["ciudad_nombre"]},
                        "shape": "Scalar"
                    }
                    slots["ciudad"] = intent["slots"]["ciudad"]
                    
                    # 🆕 MARCAR QUE LA DETECCIÓN AUTOMÁTICA SE COMPLETÓ
                    session_attributes["deteccion_automatica_completada"] = "true"
                    
            except Exception as e:
                print(f"⚠️ Error en detección automática de sedes: {str(e)}")
                # Si hay error, continuar con el flujo normal
            
            # 1. PRIMERO: Verificar si hay transición pendiente
            resultado_transicion = esperando_respuesta_sedes(session_attributes, input_transcript, slots, intent)
            if resultado_transicion:
                return resultado_transicion
            
            # 3. PROCESAR RESPUESTA DE CATEGORÍA ANTES DE VALIDAR CIUDAD
            pregunta_categoria = session_attributes.get("pregunta_categoria")
            if pregunta_categoria == "pendiente":
                confirmar = get_slot_value(slots, "confirmar_mostrar_sedes")
                if confirmar:
                    confirmar_lower = confirmar.lower().strip()
                    print(f"🔍 Procesando respuesta de categoría: '{confirmar_lower}'")

                    # Verificar si mencionó directamente una categoría válida
                    categorias_disponibles = obtener_categorias_por_linea("bodytech")
                    categorias_normalizadas = [normalizar_nombre(c) for c in categorias_disponibles]
                    input_normalizado = normalizar_nombre(confirmar_lower)

                    # Si el usuario escribió directamente una categoría válida
                    if input_normalizado in categorias_normalizadas:
                        print(f"✅ Usuario escribió directamente una categoría válida: '{confirmar_lower}'")
                        session_attributes["pregunta_categoria"] = "si"

                        # Establecer la categoría en el slot para que se procese inmediatamente
                        categoria_original = categorias_disponibles[categorias_normalizadas.index(input_normalizado)]

                        # Actualizar el slot de categoría
                        intent["slots"]["categoria"] = {
                            "value": {
                                "originalValue": categoria_original,
                                "resolvedValues": [categoria_original],
                                "interpretedValue": categoria_original
                            },
                            "shape": "Scalar"
                        }

                        # Actualizar slots locales
                        slots["categoria"] = intent["slots"]["categoria"]

                        print(f"✅ Categoría establecida automáticamente: '{categoria_original}'")
                        # Continuar con el flujo normal (no hacer return aquí)

                    # Validar respuestas para "Por categoría"
                    elif any(p in confirmar_lower for p in ["categoría", "categoria", "por categoria", "por categoría"]):
                        session_attributes["pregunta_categoria"] = "si"
                    # Validar respuestas para "Todas"
                    elif any(p in confirmar_lower for p in ["todas", "ver todas", "mostrar todas"]):
                        session_attributes["pregunta_categoria"] = "no"
                        print("✅ Usuario eligió mostrar TODAS las sedes")
                    else:
                        # Respuesta no reconocida - mostrar error específico
                        print(f"❌ Respuesta no reconocida: '{confirmar_lower}'")
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                                "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    "🤔 No entendí tu respuesta.\n\n"
                                    "¿Cómo prefieres ver las sedes?\n\n"
                                    "✅ **Respuestas válidas:**\n"
                                    "🔸 **'Por categoría'** → Para ver organizadas por tipo\n"
                                    "🔸 **'Todas'** → Para ver lista completa\n\n"
                                    "💬 **Escribe exactamente:** 'Por categoría' o 'Todas'"
                                )
                            }]
                        }
                else:
                    # Si no hay valor, vuelve a elicitar el slot
                    print("❌ No hay valor en confirmar_mostrar_sedes")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                            "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                "¿Cómo te gustaría ver las sedes?\n\n"
                                "🎯 **'Por categoría'** - Te muestro las sedes organizadas por tipo\n"
                                "📋 **'Todas'** - Te muestro todas las sedes de una vez\n\n"
                                "💬 **Responde:** 'Por categoría' o 'Todas'"
                            )
                        }]
                    }

            # 4. VALIDAR CIUDAD SOLO CUANDO SEA NECESARIO Y NO HAYA DETECCIÓN AUTOMÁTICA
            if (pregunta_categoria != "pendiente" and 
                not session_attributes.get("consultando_horarios") and 
                session_attributes.get("deteccion_automatica_completada") != "true"):
                
                ciudad_id, ciudad_nombre, session_attributes, respuesta_ciudad = validar_ciudad_usuario(
                    slots, session_attributes, input_transcript, intent
                )

                # Si hay error en validación de ciudad, retornar error
                if respuesta_ciudad:
                    return respuesta_ciudad
            else:
                # Si todavía está pendiente la respuesta de categoría o hay detección automática, usar datos existentes
                ciudad_id = session_attributes.get("ciudad_id")
                ciudad_nombre = session_attributes.get("ciudad_nombre")

            # 5. CONTINUAR CON EL FLUJO NORMAL
            ciudad = session_attributes.get("ciudad") or get_slot_value(slots, "ciudad")
            categoria = get_slot_value(slots, "categoria")
            sede_seleccionada = get_slot_value(slots, "Sede")
            confirmar_horarios = get_slot_value(slots, "confirmar_mostrar_sedes")
            # Manejar transiciones primero
            tipo_transicion = get_slot_value(slots, "tipo_transicion")
            
            if session_attributes.get("esperando_transicion_sedes") == "true":
                input_lower = input_transcript.lower().strip()
                
                if input_lower == "1":
                    tipo_transicion = "otra_ciudad"
                elif input_lower == "2":
                    tipo_transicion = "otra_sede"
                elif input_lower == "3":
                    tipo_transicion = "no"
                else:
                    contenido = (
                        "🤔 No entendí tu respuesta. Por favor, selecciona una opción válida:\n\n"
                        "1️⃣ Otra ciudad\n"
                        "2️⃣ Otra sede\n"
                        "🏠 M Menú principal\n"
                        "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                    )
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{"contentType": "PlainText", "content": contenido}]
                    }
            # Procesar transiciones
            if tipo_transicion:
                session_attributes.pop("esperando_transicion_sedes", None)
                
                if tipo_transicion == "otra_ciudad":
                    # Limpiar todo y empezar desde cero
                    keys_to_remove = ["ciudad", "ciudad_id", "ciudad_nombre", "pregunta_categoria", "consultando_horarios", "deteccion_automatica_completada"]
                    for key in keys_to_remove:
                        session_attributes.pop(key, None)
                    
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                            "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{"contentType": "PlainText", "content": "¡Perfecto! 🌎 ¿En qué ciudad deseas consultar las sedes?"}]
                    }
                
                elif tipo_transicion == "otra_sede":
                    # Mantener ciudad, limpiar proceso de horarios
                    keys_to_remove = ["pregunta_categoria", "consultando_horarios", "deteccion_automatica_completada"]
                    for key in keys_to_remove:
                        session_attributes.pop(key, None)
                    
                    # Limpiar la bandera de transición
                    session_attributes.pop("esperando_transicion_sedes", None)
    
                    # Obtener ciudad actual
                    ciudad_actual = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad") or get_slot_value(slots, "ciudad")
                    
                    if not ciudad_actual:
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                                "intent": {"name": "ConsultarSedes", "state": "InProgress", "slots": {}},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{"contentType": "PlainText", "content": "¿En qué ciudad deseas consultar las sedes?"}]
                        }
                    
                    # forzar que el flujo reinicie desde el principio
                    session_attributes["ciudad"] = ciudad_actual
                    session_attributes["pregunta_categoria"] = None  # Esto hará que pregunte por categoría
                    
                    # Actualizar el slot de ciudad para que sea procesado por el flujo normal
                    slots["ciudad"] = {
                        "value": {
                            "originalValue": ciudad_actual,
                            "resolvedValues": [ciudad_actual], 
                            "interpretedValue": ciudad_actual
                        },
                        "shape": "Scalar"
                    }
    
                    # Limpiar tipo_transicion para evitar bucles
                    if "tipo_transicion" in slots:
                        slots["tipo_transicion"] = None
    
                    print(f" Transición otra_sede procesada, continuando con flujo normal para {ciudad_actual}")
                
                elif tipo_transicion == "no":
                    # Limpiar todo y enviar pregunta final
                    keys_to_remove = ["en_flujo_activo", "ciudad", "ciudad_id", "ciudad_nombre", "pregunta_categoria", "consultando_horarios", "esperando_transicion_sedes", "deteccion_automatica_completada"]
                    for key in keys_to_remove:
                        session_attributes.pop(key, None)

                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitIntent"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                "¡Gracias por usar nuestro servicio! 🌟\n\n"
                                "¿En qué puedo ayudarte?\n\n"
                                "📄 Preguntas frecuentes sobre Bodytech\n"
                                "🏢 Consultar sedes y horarios\n"
                                "🏃‍♂️ Clases grupales disponibles\n"
                                "📅 Información de tu plan\n"
                                "👥 Consultar invitados\n"
                                "�� Información sobre referidos\n"
                                "🧾 Consultar incapacidades\n"
                                "🛍️ Información de ventas\n"
                                "❄️ Consultar congelaciones\n"
                                "¿Sobre qué tema te gustaría que te ayude?"
                            )
                        }]
                    }
            # 1. Preguntar por ciudad si no está Y NO hay detección automática
            if (not ciudad and 
                not session_attributes.get("finalizo_consultar_sedes") and 
                session_attributes.get("deteccion_automatica_completada") != "true"):
                
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
            
            # 🆕 Si hay detección automática pero no se estableció ciudad, usar datos detectados
            if (session_attributes.get("deteccion_automatica_completada") == "true" and 
                not ciudad and session_attributes.get("ciudad_nombre")):
                
                ciudad = session_attributes.get("ciudad_nombre")
                ciudad_id = session_attributes.get("ciudad_id")
                ciudad_nombre = session_attributes.get("ciudad_nombre")
                session_attributes["ciudad"] = ciudad
                
                print(f"🎯 Usando datos de detección automática: {ciudad_nombre} (ID: {ciudad_id})")

            # 2. Preguntar si desea filtrar por categoría (solo la primera vez)
            if pregunta_categoria is None:
                brand_id = 1  # Solo Bodytech
                sedes = consultar_sedes_por_ciudad_id(ciudad_id)
                sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                if not sedes_nombres:
                    return responder(
                        f"No se encontraron sedes para la ciudad {ciudad_nombre}.",
                        session_attributes, intent_name, fulfillment_state="Fulfilled"
                    )
                session_attributes["pregunta_categoria"] = "pendiente"
                
                # Obtener categorías reales disponibles para Bodytech
                categorias_disponibles = obtener_categorias_por_linea("bodytech")
                categorias_texto = "\n".join(f"   • {cat}" for cat in categorias_disponibles)
                
                # Mostrar pregunta sobre categorías
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
                        "content": (
                            f"Encontré {len(sedes_nombres)} sedes en {ciudad_nombre.title()}.\n\n"
                            "¿Cómo te gustaría ver las sedes?\n\n"
                            "🎯 **'Por categoría'** - Te muestro las sedes organizadas por tipo:\n"
                            f"{categorias_texto}\n\n"
                            "📋 **'Todas'** - Te muestro todas las sedes de una vez\n\n"
                            "💬 **Responde:** 'Por categoría' o 'Todas'"
                        )
                    }]
                }

            # 4. Procesar respuesta a la pregunta de categoría
            if pregunta_categoria == "pendiente":
                confirmar = get_slot_value(slots, "confirmar_mostrar_sedes")
                if confirmar:
                    confirmar_lower = confirmar.lower().strip()
                    print(f"🔍 Procesando respuesta de categoría: '{confirmar_lower}'")
                    
                    # Verificar si mencionó directamente una categoría válida
                    categorias_disponibles = obtener_categorias_por_linea("bodytech")
                    categorias_normalizadas = [normalizar_nombre(c) for c in categorias_disponibles]
                    input_normalizado = normalizar_nombre(confirmar_lower)
                    
                    # Si el usuario escribió directamente una categoría válida
                    if input_normalizado in categorias_normalizadas:
                        print(f"✅ Usuario escribió directamente una categoría válida: '{confirmar_lower}'")
                        session_attributes["pregunta_categoria"] = "si"
                        
                        print(f"🔍 Categorías disponibles: {categorias_disponibles}")
                        print(f"🔍 Categorías normalizadas: {categorias_normalizadas}")
                        print(f"🔍 Input normalizado: '{input_normalizado}'")
                        
                        # Si el usuario escribió directamente una categoría válida
                    if input_normalizado in categorias_normalizadas:
                        print(f"✅ Usuario escribió directamente una categoría válida: '{confirmar_lower}'")
                        session_attributes["pregunta_categoria"] = "si"
                        
                        # Establecer la categoría en el slot para que se procese inmediatamente
                        categoria_original = categorias_disponibles[categorias_normalizadas.index(input_normalizado)]
                        
                        # Actualizar el slot de categoría
                        intent["slots"]["categoria"] = {
                            "value": {
                                "originalValue": categoria_original,
                                "resolvedValues": [categoria_original],
                                "interpretedValue": categoria_original
                            },
                            "shape": "Scalar"
                        }
                        
                        # Actualizar slots locales
                        slots["categoria"] = intent["slots"]["categoria"]
                        
                        print(f"✅ Categoría establecida automáticamente: '{categoria_original}'")
                        # Continuar con el flujo normal (no hacer return aquí)
                        
                    # Validar respuestas para "Por categoría"
                    elif any(p in confirmar_lower for p in ["categoría", "categoria", "por categoria", "por categoría"]):
                        session_attributes["pregunta_categoria"] = "si"
                    # Validar respuestas para "Todas"
                    elif any(p in confirmar_lower for p in ["todas", "ver todas", "mostrar todas"]):
                        session_attributes["pregunta_categoria"] = "no"
                    else:
                        # Respuesta no reconocida - mostrar error específico
                        print(f"❌ Respuesta no reconocida: '{confirmar_lower}'")
                        return {
                            "sessionState": {
                                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                                "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                                "sessionAttributes": session_attributes
                            },
                            "messages": [{
                                "contentType": "PlainText",
                                "content": (
                                    "🤔 No entendí tu respuesta.\n\n"
                                    "¿Cómo prefieres ver las sedes?\n\n"
                                    "✅ **Respuestas válidas:**\n"
                                    "🔸 **'Por categoría'** → Para ver organizadas por tipo\n"
                                    "🔸 **'Todas'** → Para ver lista completa\n\n"
                                    "💬 **Escribe exactamente:** 'Por categoría' o 'Todas'"
                                )
                            }]
                        }
                else:
                    # Si no hay valor, vuelve a elicitar el slot
                    print("❌ No hay valor en confirmar_mostrar_sedes")
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "confirmar_mostrar_sedes"},
                            "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                "¿Cómo te gustaría ver las sedes?\n\n"
                                "🎯 **'Por categoría'** - Te muestro las sedes organizadas por tipo\n"
                                "📋 **'Todas'** - Te muestro todas las sedes de una vez\n\n"
                                "💬 **Responde:** 'Por categoría' o 'Todas'"
                            )
                        }]
                    }

            # 5. Si quiere por categoría, elicita el slot categoria y filtra
            if session_attributes.get("pregunta_categoria") == "si":
                categorias = obtener_categorias_por_linea("bodytech")
                categorias_normalizadas = [normalizar_nombre(c) for c in categorias]
                categoria_usuario = normalizar_nombre(categoria) if categoria else None
                print("DEBUG categorias:", categorias)
                print("DEBUG categorias_normalizadas:", categorias_normalizadas)
                print("DEBUG categoria_usuario:", categoria_usuario)
                print("DEBUG categoria slot:", categoria)
                brand_id = 1  # Solo Bodytech

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
                            "content": (
                                f"¡Perfecto! 🎯\n\n"
                                f"**Categorías disponibles en {ciudad_nombre}:**\n\n"
                                + "\n".join(f"• {cat}" for cat in categorias)
                                + "\n\n💬 ¿Cuál categoría deseas consultar?"
                            )
                        }]
                    }
                    
                # Consulta sedes por ciudad y categoría
                categoria_valida = categorias[categorias_normalizadas.index(categoria_usuario)]
                id_categoria = obtener_id_categoria_por_nombre(categoria_valida, brand_id)
                print(f"DEBUG: Consultando sedes para brand_id={brand_id}, id_categoria={id_categoria}, ciudad_id={ciudad_id}")
                
                if not id_categoria:
                    return responder(
                        "No se encontró la categoría seleccionada.",
                        session_attributes,
                        intent_name,
                        fulfillment_state="Fulfilled"
                    )
                    
                sedes = consultar_sedes_por_ciudad_id_linea(brand_id, id_categoria, ciudad_id)
                print(f"DEBUG: Resultado sedes={sedes}")
                sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                
                mensaje = (
                    f"🏢 **Sedes de {categoria_valida.title()} en {ciudad_nombre.title()}:**\n\n"
                    + "\n".join(f"• {s}" for s in sedes_nombres)
                    if sedes_nombres else f"❌ No se encontraron sedes de {categoria_valida} en {ciudad_nombre}."
                )
            else:
                # Solo consulta sedes por ciudad (todas las categorías)
                brand_id = 1  # Solo Bodytech
                sedes = consultar_sedes_por_ciudad_id(ciudad_id)
                sedes_nombres = [s['sede_nombre'] for s in sedes] if sedes and isinstance(sedes[0], dict) else sedes
                
                mensaje = (
                    f"🏢 **Todas las sedes en {ciudad_nombre.title()}:**\n\n"
                    + "\n".join(f"• {s}" for s in sedes_nombres)
                    if sedes_nombres else f"❌ No se encontraron sedes en {ciudad_nombre}."
                )
            
            # 6. Preguntar si desea consultar horarios de alguna sede
            if not session_attributes.get("consultando_horarios"):
                session_attributes["consultando_horarios"] = "preguntando"
                mensaje_completo = f"{mensaje}\n\n¿Deseas consultar los horarios de alguna sede específica? 🕐\n\n"
                mensaje_completo += "Si deseas consultar los horarios, escribe el nombre de la sede directamente.\n"
                mensaje_completo += "Recuerda que puedes volver al Menú principal escribiendo 🏠\"M\".\n\n"
                mensaje_completo += "💬 *Ejemplo:* 'Chapinero' o 'M'"

                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "Sede"},
                        "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{"contentType": "PlainText", "content": mensaje_completo}]
                }

            # 7. Procesar selección de sede para horarios
            if session_attributes.get("consultando_horarios") in ["preguntando", "eligiendo_sede"]:
                respuesta_usuario = input_transcript.strip().lower()
                sedes_nombres = [s['sede_nombre'].lower() for s in sedes] if sedes and isinstance(sedes[0], dict) else [s.lower() for s in sedes]

                # Opción válida: nombre de sede
                if respuesta_usuario in sedes_nombres:
                    sede_id = obtener_id_sede(respuesta_usuario)
                    if not sede_id:
                        return responder(
                            f"No reconozco la sede '{respuesta_usuario}'. Por favor, selecciona una de las sedes disponibles:\n\n"
                            + "\n".join(f"• {s}" for s in sedes_nombres),
                            session_attributes, intent_name
                        )
                    horarios = consultar_horarios_sede(sede_id)
                    if not horarios:
                        mensaje_final = f"No se encontraron horarios para la sede {respuesta_usuario.title()}."
                    else:
                        mensaje_final = respuesta_bedrock("ConsultarSedes", horarios)
                        if not mensaje_final or not mensaje_final.strip():
                            mensaje_final = f"Horarios de la sede {respuesta_usuario.title()} consultados exitosamente."
                    # Preguntar por más consultas
                    session_attributes["esperando_transicion_sedes"] = "true"
                    session_attributes["consultando_horarios"] = "eligiendo_sede"
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_transicion"},
                            "intent": {"name": intent_name, "state": "InProgress", "slots": {}},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                f"{mensaje_final}\n\n"
                                "¿Deseas hacer otra consulta de sedes? 🏢\n\n"
                                "Selecciona una opción:\n"
                                "1️⃣ Otra ciudad\n"
                                "2️⃣ Otra sede\n"
                                "3️⃣ No gracias\n\n"
                                "🏠 M Menú principal\n"
                                "💬 Escribe el nombre de la sede, recuerda que puedes elegir M para volver al menú principal, selecciona una opción."
                            )
                        }]
                    }

                # Respuesta ambigua
                else:
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "Sede"},
                            "intent": {"name": intent_name, "slots": slots, "state": "InProgress"},
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                "🤔 No entendí tu respuesta. Por favor, escribe el nombre de una sede válida, 'No' o marca la opción 2 para finalizar.\n\n"
                                "💬 *Ejemplo:* 'Chapinero' o 'No'"
                            )
                        }]
                    }

            # 9. Finalizar flujo - Solo mostrar sedes sin horarios
            # Limpiar atributos de sesión y enviar pregunta final
            session_attributes.pop("en_flujo_activo", None)
            session_attributes.pop("pregunta_categoria", None)
            session_attributes.pop("consultando_horarios", None)

            # Validación centralizada SOLO aquí
            respuesta_forzada = validar_y_forzar_flujo_ciudad(
                intent_name, slots, session_attributes, input_transcript, intent, flujo_grupales_por_ciudad
            )
            if respuesta_forzada:
                return respuesta_forzada

            return responder_con_pregunta_final(mensaje, session_attributes, intent_name)
        # -----------------------------
        # FLUJO: SALUDO + HÁBEAS DATA
        # -----------------------------
        if intent_name == "SaludoHabeasData" and session_attributes.get("esperando_respuesta_final") == "true":
            print("🔍 Interceptando SaludoHabeasData tras pregunta final, mostrando menú de ayuda")
            session_attributes.pop("esperando_respuesta_final", None)
            keys_to_remove = [
                "en_flujo_activo", "clase_display", "slots_previos",
                "esperando_transicion_grupales", "esperando_info_invitados", 
                "esperando_info_incapacidad", "esperando_info_referidos"
            ]
            for key in keys_to_remove:
                session_attributes.pop(key, None)
            return {
            "sessionState": {
                "dialogAction": {"type": "ElicitIntent"},
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": (
                "¡Perfecto! 😊 ¿En qué te puedo ayudar?\n\n"
                "Algunas opciones:\n"
                "📄 Preguntas frecuentes sobre Bodytech\n"
                "🏢 Consultar sedes y horarios\n"
                "🏃‍♂️ Clases grupales disponibles\n"
                "📅 Información de tu plan\n"
                "👥 Consultar invitados\n"
                "🏆 Información sobre referidos\n"
                "🧾 Consultar incapacidades\n"
                "🛍️ Información de ventas\n\n"
                "❄️ Consultar congelaciones\n\n"
                "¿Sobre qué tema te gustaría que te ayude?"
                )
            }]
            }
        if intent_name == "SaludoHabeasData":
            session_attributes["en_flujo_activo"] = intent_name
            saludos_validos = ["hola", "buenas", "saludos", "hey", "qué tal", "buenos días", "buenas tardes"]

            if session_attributes.get("acepto_politicas") == "true":
                # SOLO procesar si realmente es un saludo válido
                if any(s in input_transcript.lower() for s in saludos_validos):
                    return responder("¡Hola nuevamente! ¿En qué más puedo ayudarte?", session_attributes, intent_name)
                else:
                    # Si no es un saludo válido Y no es post-grupales, mostrar menú principal
                    if not session_attributes.get("ultimo_intent_completado") == "ConsultaGrupales":
                        print("⚠️ Frase clasificada como saludo pero no parece un saludo real. Mostrando menú principal.")
                        return mostrar_menu_principal(session_attributes)

            if session_attributes.get("politicas_mostradas") == "true":
                if any(p in input_transcript for p in ["si", "sí", "acepto", "de acuerdo", "vale", "claro", "ok", "bueno", "listo", "está bien","por supuesto"]):
                    session_attributes["acepto_politicas"] = "true"
                    session_attributes.pop("politicas_mostradas", None)
                    
                    # 🆕 MOSTRAR MENÚ PRINCIPAL AUTOMÁTICAMENTE
                    print("✅ Políticas aceptadas - Mostrando menú principal")
                    return mostrar_menu_principal(session_attributes)

                if any(p in input_transcript for p in [
                    "no", "rechazo", "no acepto", "no deseo continuar", 
                    "no quiero continuar", "no deseo", "no quiero",
                    "decline", "rechazar", "olvidalo", "claro que no", 
                    "por supuesto que no", "despedida", "adiós", "bye",
                    "no quiero nada", "cancelar", "salir"
                ]):
                    return terminar_sin_calificacion(
                        "Gracias por contactarte con nosotros. Lamentablemente no podemos continuar si no aceptas nuestras políticas de tratamiento de datos. ¿Si deseas continuar escribe 'acepto' o 'si' ",
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
        
        # ========================================
        # VALIDACIONES POST-POLÍTICAS
        # ========================================
        
        # Solo aplicar estas validaciones después de aceptar políticas
        if session_attributes.get("acepto_politicas") == "true":
            
            # 1. Verificar timeout de sesión (3 min + 2 min)
            respuesta_timeout = manejar_timeout_sesion(session_attributes, input_transcript)
            if respuesta_timeout:
                return respuesta_timeout
            
            # 2. Validar que el input sea texto coherente (no caracteres sin sentido)
            if not es_input_valido(input_transcript):
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitIntent"},
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            "🤔 No entendí tu mensaje. Por favor, escribe de forma clara.\n\n"
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
                            "❄️ Consultar congelaciones\n\n"
                            "¿Sobre qué tema te gustaría que te ayude?"
                        )
                    }]
                }

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
            session_attributes.pop("esperando_respuesta_final", None)
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

                 # MANEJAR CORRECTAMENTE CUANDO NO HAY DATOS
                if error_msg:
                    print(f"❌ Error consultando plan: {error_msg}")
                    
                    #  CONFIGURAR PREGUNTA DE OTRO DOCUMENTO CORRECTAMENTE
                    session_attributes["preguntando_otro_documento"] = "true"
                    session_attributes.pop("cambiando_documento", None)  # Limpiar flag conflictivo
                    
                    return {
                        "sessionState": {
                            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "document_number"},
                            "intent": {
                                "name": intent_name,
                                "state": "InProgress",
                                "slots": {}  # Limpiar slots
                            },
                            "sessionAttributes": session_attributes
                        },
                        "messages": [{
                            "contentType": "PlainText",
                            "content": (
                                f"{error_msg}\n\n"
                                "¿Quieres consultar con otro documento?\n\n"
                                "�� **Responde:** 'Sí' para usar otro documento o 'No' para finalizar"
                            )
                        }]
                    }

                # Si hay datos, procesar normalmente
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
                # ✅ RESETEAR CONTADOR AL INICIAR FLUJO EXITOSO
                from utils import resetear_contador_no_reconocidas
                resetear_contador_no_reconocidas(session_attributes)
                
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
            from utils import incrementar_contador_no_reconocidas, debe_ofrecer_asesor, ofrecer_hablar_con_asesor, es_input_valido
            
            # Verificar si el input es válido antes de incrementar contador
            if not es_input_valido(input_transcript):
                print(f"🚫 Input inválido detectado en FallbackIntent: '{input_transcript}'")
                return mostrar_menu_principal(session_attributes)
            
            contador = incrementar_contador_no_reconocidas(session_attributes)
            
            if debe_ofrecer_asesor(session_attributes):
                print("🤖 Ofreciendo hablar con asesor desde FallbackIntent")
                return ofrecer_hablar_con_asesor(session_attributes)
            
            return mostrar_menu_principal(session_attributes)
        
        # -----------------------------
        # FLUJO: Ingresos de Compañia
        # -----------------------------
        if intent_name == "Ingresos":
            session_attributes["en_flujo_activo"] = intent_name
            print("DEBUG slots recibidos:", slots)
            tipo = get_slot_value(slots, "tipo_consulta")
            sede_nombre = get_slot_value(slots, "sede")
            confirmar_segmento = get_slot_value(slots, "confirmar_mostrar_sedes")
            segmento = get_slot_value(slots, "segmento")
            Fecha = get_slot_value(slots, "Fecha")
            fecha_fin = get_slot_value(slots, "fecha_fin")
            sede_nombre = sede_nombre.split(",")[0].strip().lower() if sede_nombre else None
            sede_nombre_normalizado = normalizar_nombre(sede_nombre) if sede_nombre else None
            sede_id = obtener_id_sede(sede_nombre_normalizado) if sede_nombre_normalizado else None
            
            segmento_map = { "Administrativo": 90, "Corporativo": 5, "Masivo": 4}
            segmento_id = segmento_map.get(segmento) if segmento else None
            
            print("DEBUG tipo:", tipo)
            print("DEBUG fecha:", Fecha)
            print("DEBUG sede_nombre:", sede_nombre)
            print("DEBUG sede_id:", sede_id)
            
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

            tipo_normalizado = tipo.lower().strip() if tipo else ""
            print("DEBUG tipo_normalizado:", tipo_normalizado)
            print("DEBUG sede_nombre:", sede_nombre)

            if tipo_normalizado in ["por sede", "sede"] and not sede_nombre:
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

            # Validar y normalizar fechas (solo si ambas están presentes)
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
            
            # Solo usar Bodytech (brand_id = 1)
            query = armar_consulta_ingresos("bodytech", tipo, Fecha, fecha_fin, sede_id, segmento_id)
            resultado = ejecutar_consulta(query)

            # Armar resumen para Bedrock
            resumen = {
                "linea": "bodytech",
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
            
        # -----------------------------
        # FLUJO: Consulta de Horarios (Clasificar)
        # -----------------------------
        if intent_name == "ConsultaHorarios":
            
            palabras_grupales = ["clases", "grupales", "actividades", "clase", "yoga", "pilates", "spinning"] 
            input_lower = input_transcript.lower()
            slots = intent.get("slots", {})
            tipo_horario = get_slot_value(slots, "tipo_horario")
            # Procesar la respuesta del usuario
            tipo_lower = tipo_horario.lower().strip() if tipo_horario else ""
            print(f"🔍 Procesando tipo_horario: '{tipo_lower}'")
            from services import get_actividades_map_normalizado,obtener_id_actividad

            actividades_map_normalizado = get_actividades_map_normalizado()

            # 2. Detectar si el input contiene una clase (aunque esté mal escrita)
            clase_detectada = None
            for clase_norm in actividades_map_normalizado.keys():
                if clase_norm in input_lower:
                    clase_detectada = clase_norm
                    break
                # Similitud generativa: usa tu función de similitud avanzada
                if obtener_id_actividad(input_lower) == actividades_map_normalizado[clase_norm]:
                    clase_detectada = clase_norm
                    break
            tipo_horario = get_slot_value(slots, "tipo_horario")
            tipo_lower = tipo_horario.lower().strip() if tipo_horario else ""
            
            if any(palabra in tipo_lower for palabra in ["sede", "gimnasio", "atencion", "atención", "general"]):
                print("🔄 Redirigiendo a ConsultarSedes")
                # Limpiar datos de esta intención
                session_attributes.pop("en_flujo_activo", None)
                
                # PRESERVAR slots existentes para ConsultarSedes
                slots_preservados = {}
                for slot_name, slot_value in slots.items():
                    if slot_name != "tipo_horario" and slot_value:  # Excluir el slot de tipo_horario
                        slots_preservados[slot_name] = slot_value
                
                # Redirigir a ConsultarSedes
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},  
                        "intent": {
                            "name": "ConsultarSedes",
                            "state": "InProgress", 
                            "slots": slots_preservados
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¡Perfecto! Te ayudo a consultar información de sedes y horarios de atención 🏢\n\n¿En qué ciudad deseas consultar las sedes?"
                    }]
                }
            # 3. Si se detecta palabra grupal o clase, redirige a ConsultaGrupales
            elif any(palabra in input_lower for palabra in palabras_grupales) or clase_detectada:
                print("🔄 Redirigiendo automáticamente a ConsultaGrupales por palabra clave o clase detectada")
                slots_preservados = {}
                for slot_name, slot_value in slots.items():
                    if slot_name != "tipo_horario" and slot_value:  # Excluir el slot de tipo_horario
                        slots_preservados[slot_name] = slot_value

                resultado = extraer_y_validar_slots_grupales(input_transcript, session_attributes, {
                    "name": "ConsultaGrupales",
                    "slots": slots
                })
                
                if "sessionState" in resultado:
                    return resultado
                
                if "error" in resultado:
                    return resultado["error"]

                # Poblar los slots para ConsultaGrupales
                slots_nuevos = {}
                if resultado["ciudad_id"]:
                    slots_nuevos["ciudad"] = {
                        "value": {"interpretedValue": resultado["ciudad_nombre"]},
                        "shape": "Scalar"
                    }
                    session_attributes["ciudad_id"] = str(resultado["ciudad_id"])
                    session_attributes["ciudad_nombre"] = resultado["ciudad_nombre"]

                if resultado["sede_id"]:
                    slots_nuevos["sede"] = {
                        "value": {"interpretedValue": resultado["sede_nombre"]},
                        "shape": "Scalar"
                    }
                    session_attributes["sede_id"] = str(resultado["sede_id"])
                    session_attributes["sede_nombre"] = resultado["sede_nombre"]


                if resultado["clase_id"]:
                    slots_nuevos["clase"] = {
                        "value": {"interpretedValue": resultado["clase_nombre"]},
                        "shape": "Scalar"
                    }
                    session_attributes["clase_id"] = str(resultado["clase_id"])
                    session_attributes["clase_nombre"] = resultado["clase_nombre"]

                if resultado["fecha"]:
                    slots_nuevos["fecha"] = {
                        "value": {"interpretedValue": resultado["fecha"]},
                        "shape": "Scalar"
                    }

                # Redirigir a ConsultaGrupales
                return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                        "intent": {
                            "name": "ConsultaGrupales",
                            "state": "InProgress",
                            "slots": slots_preservados
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": "¡Perfecto! Te ayudo a consultar los horarios de clases grupales 🏃‍♂️\n\n¿En qué ciudad te encuentras?"
                    }]
                }
 
            if not slots.get("tipo_horario"):
                return manejar_consulta_horarios(intent, session_attributes, slots, input_transcript)
            # Llama a la función centralizada
            print("Entrando a ConsultaHorarios")
            respuesta = manejar_consulta_horarios(intent, session_attributes, slots, input_transcript)
            print("Respuesta de manejar_consulta_horarios:", respuesta)
            if respuesta:
                print("Return inmediato por respuesta")
                return respuesta
            print("Sigue el flujo de ConsultaHorarios")
            session_attributes["en_flujo_activo"] = intent_name
            
            # Si no se reconoce la respuesta, pregunta por tipo_horario
            return {
                    "sessionState": {
                        "dialogAction": {"type": "ElicitSlot", "slotToElicit": "tipo_horario"},
                        "intent": {
                            "name": intent_name,
                            "slots": slots,
                            "state": "InProgress"
                        },
                        "sessionAttributes": session_attributes
                    },
                    "messages": [{
                        "contentType": "PlainText",
                        "content": (
                            "🤔 No entendí tu respuesta.\n\n"
                            "¿Qué tipo de horarios necesitas?\n\n"
                            "✅ **Respuestas válidas:**\n"
                            "🔸 'Clases grupales' → Para horarios de actividades\n"
                            "🔸 'Sede' → Para horarios de atención\n\n"
                            "💬 **Escribe tu preferencia:**"
                        )
                    }]
                }
            
                
            
    except Exception as e:
        print("❌ Error general en Lambda:", str(e))
        return responder("Lo siento, ha ocurrido un error inesperado.", {}, "FallbackIntent")


        









