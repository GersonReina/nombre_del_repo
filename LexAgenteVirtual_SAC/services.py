import boto3
import unicodedata
import json
import difflib
import re
from utils import responder, cerrar_conversacion, normalizar_fecha
from secret import obtener_secret
from redshift_utils import consultar_sedes_redshift, consultar_clases_por_sede_id
from respuestas import consultar_bedrock_generacion
from prompts import get_prompt_por_intent
# --------------------- #
# FUNCIONES AUXILIARES  #
# --------------------- #

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


###
# Respuesta Post Pregunta Adicional
###

def manejar_respuesta_post_pregunta_adicional(input_transcript, session_attributes):
    negativas = ["no", "no gracias", "nada más", "estoy bien", "ninguna","hasta luego"]
    afirmativas = ["sí", "si", "claro", "vale","listo","de acuerdo", "otra", "quiero saber"]

    if any(p in input_transcript.lower() for p in negativas):
        return cerrar_conversacion("Gracias por contactarte con nosotros. ¡Feliz día! 😊", "Despedida")

    elif any(p in input_transcript.lower() for p in afirmativas):
        # 🆕 MOSTRAR MENÚ PRINCIPAL cuando responden afirmativamente
        from utils import mostrar_menu_principal
        return mostrar_menu_principal(session_attributes)

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

####
# Consultar ID Ciudad
####

def validar_ciudad_usuario(slots, session_attributes, input_transcript, intent):
    print("🔍 ===== DEBUG VALIDAR CIUDAD =====")
    print("🔍 Slots recibidos:", slots)
    print("🔍 input_transcript:", input_transcript)
    print("🔍 session_attributes:", session_attributes)
    
    #  Si ya tenemos ciudad válida Y estamos procesando transición de sedes, usar la existente**
    if (session_attributes.get("ciudad_id") and 
        session_attributes.get("ciudad_nombre") and 
        session_attributes.get("esperando_transicion_sedes") == "true"):
        
        print("🔍 TRANSICIÓN SEDES DETECTADA: usando ciudad existente")
        print(f"🔍 Ciudad existente: {session_attributes.get('ciudad_nombre')} (ID: {session_attributes.get('ciudad_id')})")
        
        return int(session_attributes.get("ciudad_id")), session_attributes.get("ciudad_nombre"), session_attributes, None
    
    #  Si ya tenemos ciudad válida Y estamos procesando transición de grupales, usar la existente**
    if (session_attributes.get("ciudad_id") and 
        session_attributes.get("ciudad_nombre") and 
        session_attributes.get("esperando_transicion_grupales") == "true"):
        
        print("🔍 TRANSICIÓN GRUPALES DETECTADA: usando ciudad existente")
        print(f"🔍 Ciudad existente: {session_attributes.get('ciudad_nombre')} (ID: {session_attributes.get('ciudad_id')})")
        
        return int(session_attributes.get("ciudad_id")), session_attributes.get("ciudad_nombre"), session_attributes, None
    
    # Si ya tenemos ciudad válida Y estamos en flujo activo de ConsultaGrupales, NO revalidar**
    if (session_attributes.get("ciudad_id") and 
        session_attributes.get("ciudad_nombre") and
        session_attributes.get("en_flujo_activo") == "ConsultaGrupales"):
        
        # Verificar si el input es una sede conocida antes de revalidar como ciudad
        if input_transcript and len(input_transcript.split()) <= 2:
            sede_id_test = obtener_id_sede(normalizar_nombre(input_transcript.strip()))
            if sede_id_test:
                print(f"🔍 Input '{input_transcript}' es una SEDE conocida (ID: {sede_id_test}), NO revalidar como ciudad")
                print(f"🔍 Manteniendo ciudad existente: {session_attributes.get('ciudad_nombre')}")
                return int(session_attributes.get("ciudad_id")), session_attributes.get("ciudad_nombre"), session_attributes, None
        
        print("🔍 FLUJO GRUPALES ACTIVO: usando ciudad existente")
        print(f"🔍 Ciudad existente: {session_attributes.get('ciudad_nombre')} (ID: {session_attributes.get('ciudad_id')})")
        
        return int(session_attributes.get("ciudad_id")), session_attributes.get("ciudad_nombre"), session_attributes, None
    
    ciudad_raw = ""

    # 1. Intenta extraer de los slots
    if slots:
        ciudad_slot = slots.get("ciudad")
        if ciudad_slot and isinstance(ciudad_slot, dict):
            ciudad_raw = (
                ciudad_slot.get("value", {}).get("interpretedValue")
                or ciudad_slot.get("value", {}).get("originalValue")
                or ""
            )
            print("🔍 ciudad_raw extraído de slots:", ciudad_raw)

    # 2. Si el slot está vacío, usa el input_transcript
    if not ciudad_raw and input_transcript:
        ciudad_raw = input_transcript.strip().lower()

    print("🔍 ciudad_raw extraído de input_transcript:", ciudad_raw)

    print("📌 ciudad_raw FINAL:", ciudad_raw)
    print("📌 tipo de ciudad_raw:", type(ciudad_raw))
    print("📌 longitud de ciudad_raw:", len(ciudad_raw) if ciudad_raw else 0)
    
    if not ciudad_raw:
        print("❌ ciudad_raw está vacío, retornando None")
        return None, None, session_attributes, None
    
    # Mapeo simplificado: nombre base -> ID (normalizar_nombre se encarga de las variaciones)
    ciudades_map = {
        "bogota": 1,
        "medellin": 2, 
        "soacha": 3,
        "villavicencio": 4,
        "barranquilla": 5,
        "armenia": 6,
        "tulua": 7,
        "cartagena": 8,
        "bucaramanga": 9,
        "cali": 10,
        "monteria": 11,
        "bello": 12,
        "neiva": 13,
        "palmira": 14,
        "valledupar": 15,
        "manizales": 16,
        "envigado": 18,
        "ibague": 19,
        "chia": 20,
        "dosquebradas": 21,
        "cucuta": 22,
        "pasto": 23,
        "pereira": 24,
        "tunja": 36
    }
    
    ciudades_id_nombre = {
        1: "Bogotá", 2: "Medellín", 3: "Soacha", 4: "Villavicencio", 5: "Barranquilla",
        6: "Armenia", 7: "Tuluá", 8: "Cartagena", 9: "Bucaramanga", 10: "Cali",
        11: "Montería", 12: "Bello", 13: "Neiva", 14: "Palmira", 15: "Valledupar",
        16: "Manizales", 18: "Envigado", 19: "Ibagué", 20: "Chía", 21: "Dosquebradas",
        22: "Cúcuta", 23: "Pasto", 24: "Pereira", 36: "Tunja"
    }
    
    ciudad_id = None
    ciudad_nombre = ""
    
    if not ciudad_raw: 
        return None, None, session_attributes, None
    
    
    print("🔍 ===== INICIANDO BÚSQUEDAS =====")
    
    # Normalizar entrada del usuario
    ciudad_normalizada = normalizar_nombre(ciudad_raw)
    print(f"🔍 Ciudad normalizada: '{ciudad_raw}' → '{ciudad_normalizada}'")
    
    # 1. Búsqueda exacta con normalización
    for key in ciudades_map:
        key_normalizada = normalizar_nombre(key)
        if ciudad_normalizada == key_normalizada:
            ciudad_id = ciudades_map[key]
            ciudad_nombre = ciudades_id_nombre.get(ciudad_id, key.title())
            print(f"✅ Ciudad encontrada (exacta normalizada): '{ciudad_raw}' → '{key}'")
            break
    
    # 2. Búsqueda con difflib como fallback
    if not ciudad_id:
        print("🔍 2. Búsqueda con difflib...")
        
        # ✅ AGREGAR: Lista de palabras que NO son ciudades
        palabras_excluidas = [
            'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
            'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre',
            'hoy', 'mañana', 'ayer', 'de', 'del', 'el', 'la', 'en', 'que', 'hay', 'clases',
            'saber', 'quiero', 'consultar', 'ver', 'mostrar', 'horarios', 'horario',
            'gimnasio', 'clase', 'actividad', 'plan', 'sede', 'disponible'
        ]
        
        # Antes de buscar con difflib, verificar si es palabra excluida
        if ciudad_raw.lower() in palabras_excluidas:
            print(f"🚫 Palabra excluida de búsqueda de ciudad: '{ciudad_raw}'")
            print("🔍 ===== FIN DEBUG VALIDAR CIUDAD =====")
        else:
            todas_ciudades = list(ciudades_map.keys())
            matches = difflib.get_close_matches(ciudad_raw.lower(), todas_ciudades, n=1, cutoff=0.6)
            print(f"🔍 difflib matches con cutoff 0.6: {matches}")
            if matches:
                ciudad_encontrada = matches[0]
                ciudad_id = ciudades_map[ciudad_encontrada]
                ciudad_nombre = ciudades_id_nombre.get(ciudad_id, ciudad_encontrada.title())
                print(f"✅ Ciudad corregida (difflib): '{ciudad_raw}' → '{ciudad_encontrada}'")
            else:
                print("❌ NO SE ENCONTRÓ NINGUNA COINCIDENCIA")
                print("🔍 ===== FIN DEBUG VALIDAR CIUDAD =====")
    
    # 4. Si no se encuentra, mostrar error
    if not ciudad_id:
        lista_ciudades = ", ".join(sorted(set(ciudades_id_nombre.values())))
        mensaje = f"No reconozco la ciudad '{ciudad_raw}'. Las ciudades disponibles son: {lista_ciudades}"
        respuesta = {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {
                    "name": intent.get("name"),
                    "slots": intent.get("slots", {}),
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{"contentType": "PlainText", "content": mensaje}]
        }
        return None, None, session_attributes, respuesta

    # 5. Guardar en sesión
    session_attributes["ciudad_id"] = str(ciudad_id)
    session_attributes["ciudad_nombre"] = ciudad_nombre
    print(f"✅ Ciudad mapeada: {ciudad_id} - {ciudad_nombre}")

    return ciudad_id, ciudad_nombre, session_attributes, None


###
# Consulta Tipo y Numero de Documeto  
###

def validar_documento_usuario(slots, session_attributes, input_transcript, intent):
    print("🔎 Slots recibidos:", slots)
    document_type_raw = get_slot_value(slots, "TipoDocumento") or get_slot_value(slots, "document_type")
    document_number = get_slot_value(slots, "NumeroDocumento") or get_slot_value(slots, "document_number")

    # Extraer valores de slots con estructura completa
    if slots:
        document_type_slot = slots.get("document_type") or slots.get("TipoDocumento")
        document_number_slot = slots.get("document_number") or slots.get("NumeroDocumento")
        
        if document_type_slot and isinstance(document_type_slot, dict):
            document_type_raw = (
                document_type_slot.get("value", {}).get("interpretedValue")
                or document_type_slot.get("value", {}).get("originalValue")
                or ""
            )
        if document_number_slot and isinstance(document_number_slot, dict):
            document_number = (
                document_number_slot.get("value", {}).get("interpretedValue")
                or document_number_slot.get("value", {}).get("originalValue")
                or ""
            )
    
    # Si no hay tipo de documento en slots, usar input_transcript (excepto si es solo números)
    if not document_type_raw and input_transcript and not input_transcript.strip().isdigit():
        document_type_raw = input_transcript.strip().lower()

    # ✅ NUEVO: Si no hay tipo de documento en slots pero sí en input_transcript, procesarlo
    if not document_type_raw and input_transcript and not input_transcript.strip().isdigit():
        # Si el input es un tipo de documento válido, usarlo
        input_limpio = input_transcript.strip().lower()
        tipos_documento_validos = [
            "cc", "cedula", "cedula de ciudadania", "cédula de ciudadanía",
            "ce", "cedula de extranjeria", "cédula de extranjería", 
            "pp", "pasaporte", "passport",
            "ti", "tarjeta de identidad", "tarjeta identidad",
            "ke", "carnet de extranjeria", "carnet de extranjería"
        ]
        
        if any(tipo in input_limpio for tipo in tipos_documento_validos):
            document_type_raw = input_transcript.strip()
            print(f"🔄 Tipo de documento detectado en input: '{document_type_raw}'")

    # ✅ NUEVO: Si el input es solo números y no tenemos document_number, es el número de documento
    if not document_number and input_transcript and input_transcript.strip().isdigit():
        if 4 <= len(input_transcript.strip()) <= 15:  # Longitud válida para documento
            document_number = input_transcript.strip()
            print(f"🔄 Número de documento detectado en input: '{document_number}'")
    
    # Recuperar de session_attributes si están vacíos
    if not document_type_raw:
        document_type_raw = session_attributes.get("document_type_raw", "")
    if not document_number:
        document_number = session_attributes.get("document_number", "")
        
    print("📌 document_type_raw:", document_type_raw)
    print("📌 document_number:", document_number)

    #  Mapear respuestas de botones WhatsApp
    if document_type_raw:
        mapeo_botones_whatsapp = {
            "cedula_ciudadania": "Cédula de Ciudadanía",
            "cedula_extranjeria": "Cédula de Extranjería", 
            "pasaporte": "Pasaporte",
            "tarjeta_identidad": "Tarjeta de Identidad",
            "carnet_extranjeria": "Carnet de Extranjería"
        }
        
        # Si viene del botón de WhatsApp, mapear al valor correcto
        if document_type_raw in mapeo_botones_whatsapp:
            document_type_raw = mapeo_botones_whatsapp[document_type_raw]
            print(f"🔄 WhatsApp botón mapeado: '{document_type_raw}'")

    # Mapeo de tipos de documento a IDs
    mapeo_documentos = {
        "cedula de ciudadania": 10, "cédula de ciudadanía": 10, "cedula ciudadania": 10,
        "cc": 10, "cedula": 10, "ciudadania": 10,
        "tarjeta de identidad": 50, "ti": 50, "tarjeta identidad": 50,
        "cedula de extranjeria": 20, "cédula de extranjería": 20, "cedula extranjeria": 20,
        "ce": 20, "extranjeria": 20,
        "pasaporte": 30, "passport": 30, "pp": 30,
        "carnet de extranjeria": 2, "carnet de extranjería": 2, "ke": 2, "carnet extranjeria": 2
    }
    
    document_type_id = None
    tipo_normalizado = (document_type_raw or "").strip().lower()
    
    # Buscar coincidencia en el mapeo
    for key in mapeo_documentos:
        if tipo_normalizado and key in tipo_normalizado:
            document_type_id = mapeo_documentos[key]
            break
    
    # Si no se pudo mapear, intentar recuperar de sesión
    if not document_type_id:
        document_type_id = session_attributes.get("document_type_id")
        if document_type_id:
            document_type_raw = session_attributes.get("document_type_raw", "")
    
    print("🟢 document_type_id:", document_type_id)
    
    # Guardar en sesión si se tiene información válida
    if document_type_id:
        session_attributes["document_type_id"] = str(document_type_id)
        session_attributes["document_type_raw"] = document_type_raw
        session_attributes["document_type"] = document_type_raw 
    if document_number:
        session_attributes["document_number"] = document_number

    # SI FALTA TIPO DE DOCUMENTO: Mostrar botones
    if not document_type_id:
        contenido = (
            "¿Qué tipo de documento tienes?\n\n"
            "Tipos aceptados:\n"
            "• Cédula de Ciudadanía (cc)\n"
            "• Cédula de Extranjería (ce)\n"
            "• Pasaporte (pp)\n"
            "• Tarjeta de Identidad (ti)\n"
            "• Carnet de Extranjería (ke)\n\n"
            "Puedes escribir el tipo de documento señalado en parentesis ó escribirlo directamente:"
        )
        
        respuesta = {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "document_type"},
                "intent": {
                    "name": intent.get("name"),
                    "slots": intent.get("slots", {}),
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{"contentType": "PlainText", "content": contenido}]
        }
        return None, None, session_attributes, respuesta

    if not document_number or len(document_number) < 4:
        mensaje = "Por favor, indícame tu número de documento sin espacios ni caracteres especiales:"
        respuesta = {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "document_number"},
                "intent": {
                    "name": intent.get("name"),
                    "slots": intent.get("slots", {}),
                    "state": "InProgress",
                    "confirmationState": "None"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": mensaje
            }]
        }
        return None, None, session_attributes, respuesta

    # ✅ ÉXITO: Ambos datos válidos
    session_attributes["document_type_id"] = str(document_type_id)
    session_attributes["document_number"] = document_number
    print(f"✅ Tipo documento mapeado: {document_type_id}, Número: {document_number}")

    return document_type_id, document_number, session_attributes, None

def obtener_info_sedes():
    sedes = consultar_sedes_redshift()
    if not sedes:
        return "No se encontró información de sedes en este momento."

    sedes_texto = "\n".join([f"- {s['sede_nombre']} (Categoría: {s['categoria']})" for s in sedes])
    prompt = get_prompt_por_intent("ConsultarSedes", sedes_texto)
    respuesta = consultar_bedrock_generacion(prompt)
    return respuesta

def normalizar_nombre(nombre):
    """Normaliza un nombre removiendo tildes, espacios extra y convirtiendo a minúsculas"""
    if not nombre:
        return ""
    
    # Convertir a minúsculas y quitar espacios extra
    nombre = nombre.lower().strip()
    
    # Quitar tildes y caracteres especiales
    nombre_sin_tildes = ''.join(c for c in unicodedata.normalize('NFD', nombre) 
                               if unicodedata.category(c) != 'Mn')
    
    # Quitar espacios extra y caracteres especiales
    nombre_sin_tildes = nombre_sin_tildes.replace('ñ', 'n')
    nombre_limpio = re.sub(r'[^\w\s]', '', nombre_sin_tildes)
    nombre_limpio = re.sub(r'\s+', ' ', nombre_limpio).strip()
    
    ##print(f"🔍 normalizar_nombre: '{nombre}' → '{nombre_limpio}'")
    return nombre_limpio

def obtener_id_sede(nombre_sede):
    print(f"===== DEBUG OBTENER_ID_SEDE =====")
    print(f"Input original: '{nombre_sede}'")
    
    # 🆕 VERIFICACIÓN ESPECIAL PARA DETECTAR PROBLEMA CON CENTRO MAYOR
    if "centro" in nombre_sede.lower():
        print(f"⚠️ ADVERTENCIA: Input contiene 'centro': '{nombre_sede}' - verificando contexto")
        # Solo rechazar si es "centro" individual, NO si es "centro mayor" completo
        if nombre_sede.lower().strip() == "centro":
            print(f"🛑 STOP: Input es solo 'centro' - muy ambiguo, podría ser parte de sede compuesta")
            return None
    
    # Verificar si el input es una ciudad conocida
    ciudades_conocidas = [
        "bogota", "medellin", "cali", "barranquilla", "bucaramanga", "cartagena", 
        "pereira", "armenia", "manizales", "villavicencio", "ibague", "cucuta", 
        "pasto", "tunja", "palmira", "neiva", "monteria", "valledupar", 
        "soacha", "bello", "chia", "envigado", "dosquebradas", "tulua"
    ]
    
    nombre_normalizado = normalizar_nombre(nombre_sede)
    print(f"Nombre normalizado: '{nombre_normalizado}'")
    
    # Si el input es una ciudad, retornar None inmediatamente
    if nombre_normalizado in [normalizar_nombre(c) for c in ciudades_conocidas]:
        print(f"❌ '{nombre_sede}' es una CIUDAD, no una sede. Retornando None.")
        return None
    sedes_map = {
        "ejecutivos": 90,
        "dos quebradas": 68,
        "viva villavicencio": 92,
        "bocagrande": 86,
        "llanocentro": 43,
        "titan plaza": 99,
        "calle 90": 122,
        "autopista 170": 11,
        "gran estacion": 107,
        "torre central": 41,
        "normandia": 7,
        "colina": 24,
        "carrera 11": 6,
        "autopista 135": 10,
        "bulevar": 102,
        "kennedy": 3,
        "diverplaza": 106,
        "pasadena": 35,
        "pablo vi": 34,
        "country 138": 116,
        "chipichape": 23,
        "ensueño": 123,
        "plaza central": 103,
        "terreros": 117,
        "oeste": 71,
        "floresta": 53,
        "plaza bosa": 115,
        "paseo del rio": 126,
        "santa ana": 127,
        "tulua": 91,
        "palmira": 96,
        "hayuelos": 27,
        "sultana": 40,
        "centro mayor": 8,
        "suba": 39,
        "chia": 22,
        "cacique": 85,
        "caney": 94,
        "llanogrande": 125,
        "chico": 2,
        "ibague": 72,
        "vizcaya": 47,
        "san lucas": 66,
        "fontanar": 111,
        "antares": 105,
        "villagrande": 69,
        "laureles": 44,
        "belen": 76,
        "americas": 75,
        "camino real": 77,
        "san juan": 80,
        "cucuta": 25,
        "plazuela": 36,
        "galerias": 110,
        "cabrera": 17,
        "pasto": 87,
        "mall del este": 81,
        "superior": 81,
        "manizales": 46,
        "santa maria de los angeles": 48,
        "vegas": 48,
        "pereira": 50,
        "armenia": 70,
        "robledo": 55,
        "avenida colombia": 58,
        "colombia": 58,
        "megamall": 15,
        "premium plaza": 45,
        "cedritos": 20,
        "city plaza": 59,
        "portal 80": 37,
        "chapinero": 21,
        "miramar": 32,
        "recreo": 56,
        "jardin plaza": 29,
        "viva barranquilla": 101,
        "parque washington": 112,
        "washington": 112,
        "niquia": 57,
        "caracoli": 63,
        "caribe plaza": 19,
        "calle 122 studio": 152,
        "gran manzana": 185,
        "tunja": 150,
        "connecta": 151
    }
    
    # 1. Búsqueda exacta con normalización
    for sede_key in sedes_map:
        if nombre_normalizado == normalizar_nombre(sede_key):
            resultado = sedes_map[sede_key]
            print(f"✅ ENCONTRADO EXACTO: '{nombre_sede}' → '{sede_key}' (ID: {resultado})")
            return resultado
    
    # 2. Búsqueda con difflib - PERO CON VERIFICACIONES ESPECIALES
    todas_sedes = list(sedes_map.keys())
    
    # 🆕 VERIFICAR SI EL INPUT PODRÍA SER PARTE DE UNA SEDE COMPUESTA
    palabras_problematicas = ["centro", "mayor", "plaza", "portal", "calle", "autopista", "torre", "gran"]
    if any(palabra in nombre_sede.lower() for palabra in palabras_problematicas):
        print(f"⚠️ Input contiene palabra problemática que podría ser parte de sede compuesta: '{nombre_sede}'")
        # Usar cutoff más alto para evitar falsos positivos
        matches = difflib.get_close_matches(nombre_sede.lower(), todas_sedes, n=1, cutoff=0.85)
    else:
        matches = difflib.get_close_matches(nombre_sede.lower(), todas_sedes, n=1, cutoff=0.6)
    
    if matches:
        sede_encontrada = matches[0]
        resultado = sedes_map[sede_encontrada]
        print(f"✅ ENCONTRADO (difflib): '{nombre_sede}' → '{sede_encontrada}' (ID: {resultado})")
        
        # 🆕 VERIFICACIÓN ADICIONAL: Si el match es "llanocentro" pero el input es "centro", rechazar
        if sede_encontrada == "llanocentro" and nombre_sede.lower().strip() == "centro":
            print(f"🛑 RECHAZADO: '{nombre_sede}' matched con 'llanocentro' pero es muy ambiguo")
            return None
            
        return resultado
    
    print(f"❌ NO ENCONTRADO: '{nombre_sede}'")
    print("===== FIN DEBUG OBTENER_ID_SEDE =====")
    return None

def obtener_nombre_sede_por_id(sede_id):
    """Obtiene el nombre completo de la sede dado su ID"""
    sedes_id_nombre = {
        # ID: "Nombre Completo"
        90: "Ejecutivos",
        68: "Dos Quebradas", 
        92: "Viva Villavicencio",
        86: "Bocagrande",
        43: "Llanocentro",
        99: "Titan Plaza",
        122: "Calle 90",
        11: "Autopista 170",
        107: "Gran Estacion",
        41: "Torre Central",
        7: "Normandia",
        24: "Colina",
        6: "Carrera 11",
        10: "Autopista 135",
        102: "Bulevar",
        3: "Kennedy",
        106: "Diverplaza",
        35: "Pasadena",
        34: "Pablo VI",
        116: "Country 138",
        23: "Chipichape",
        123: "Ensueño", 123:"ensueno",
        103: "Plaza Central",
        117: "Terreros",
        71: "Oeste",
        53: "Floresta",
        115: "Plaza Bosa",
        126: "Paseo del Rio",
        127: "Santa Ana",
        91: "Tulua",
        96: "Palmira",
        27: "Hayuelos",
        40: "Sultana",
        8: "Centro Mayor",
        39: "Suba",
        22: "Chia",
        85: "Cacique",
        94: "Caney",
        125: "Llanogrande",
        2: "Chico",
        72: "Ibague",
        47: "Vizcaya",
        66: "San Lucas",
        111: "Fontanar",
        105: "Antares",
        69: "Villagrande",
        44: "Laureles",
        76: "Belen",
        75: "Americas",
        77: "Camino Real",
        80: "San Juan",
        25: "Cucuta",
        36: "Plazuela",
        110: "Galerias",
        17: "Cabrera",
        87: "Pasto",
        81: "Mall del Este",
        46: "Manizales",
        48: "Santa Maria de los Angeles",
        50: "Pereira",
        70: "Armenia",
        55: "Robledo",
        58: "Avenida Colombia",
        15: "Megamall",
        45: "Premium Plaza",
        20: "Cedritos",
        59: "City Plaza",
        37: "Portal 80",
        21: "Chapinero",
        32: "Miramar",
        56: "Recreo",
        29: "Jardin Plaza",
        101: "Viva Barranquilla",
        112: "Parque Washington",
        57: "Niquia",
        63: "Caracoli",
        19: "Caribe Plaza",
        152: "Calle 122 Studio",
        185: "Gran Manzana",
        150: "Tunja",
        151: "Connecta"
    }
    return sedes_id_nombre.get(sede_id, "")

def validar_sede_usuario(slots, session_attributes, input_transcript, intent, ciudad_id):
    print("🔍 ===== DEBUG VALIDAR SEDE =====")
    print("🔍 Slots recibidos:", slots)
    print("🔍 input_transcript:", input_transcript)
    print("🔍 ciudad_id:", ciudad_id)
    
    sede_raw = ""

    # 1. Intenta extraer de los slots
    if slots:
        sede_slot = slots.get("sede")
        print("🔍 sede_slot:", sede_slot)
        if sede_slot and isinstance(sede_slot, dict):
            sede_raw = (
                sede_slot.get("value", {}).get("interpretedValue")
                or sede_slot.get("value", {}).get("originalValue")
                or ""
            )
            print("🔍 sede_raw extraído de slots:", sede_raw)

    # 2. Si el slot está vacío, usa el input_transcript
    if not sede_raw and input_transcript:
        sede_raw = input_transcript.strip().lower()
        print("🔍 sede_raw extraído de input_transcript:", sede_raw)

    print("📌 sede_raw FINAL:", sede_raw)
    
    if not sede_raw:
        print("❌ sede_raw está vacío, retornando None")
        return None, None, session_attributes, None
    
    # 3. Normalizar nombre y obtener ID de sede
    sede_normalizada = normalizar_nombre(sede_raw)
    print("🔍 sede_normalizada:", sede_normalizada)
    
    sede_id = obtener_id_sede(sede_normalizada)
    print("🔍 sede_id obtenido:", sede_id)
    
    if not sede_id:
        # Obtener lista de sedes disponibles para la ciudad
        sedes_disponibles = consultar_sedes_por_ciudad_id(ciudad_id)  # brand_id = 1 para Bodytech
        sedes_nombres = [s['sede_nombre'] for s in sedes_disponibles] if sedes_disponibles else []
        
        sedes_nombres_normalizados = [normalizar_nombre(s) for s in sedes_nombres]
        if sede_normalizada in sedes_nombres_normalizados:
            idx = sedes_nombres_normalizados.index(sede_normalizada)
            sede_real = sedes_nombres[idx]
            sede_id = obtener_id_sede(sede_real)
            if not sede_id:
                # Intenta con el nombre normalizado (por si el mapeo tiene la versión sin tilde/ñ)
                sede_id = obtener_id_sede(sede_normalizada)
            if not sede_id:
                # Intenta con el input original del usuario
                sede_id = obtener_id_sede(sede_raw)
            print(f"✅ Sede encontrada por normalización: '{sede_real}' (id: {sede_id})")
            sede_nombre_completo = obtener_nombre_sede_por_id(sede_id)
            if intent and "slots" in intent:
                intent["slots"]["sede"] = {
                    "value": {
                        "originalValue": sede_nombre_completo or sede_real,
                        "resolvedValues": [sede_nombre_completo or sede_real],
                        "interpretedValue": sede_nombre_completo or sede_real
                    },
                    "shape": "Scalar"
                }
            session_attributes["sede_id"] = str(sede_id)
            session_attributes["sede_nombre"] = sede_nombre_completo or sede_real.title()
            print(f"✅ Sede mapeada (normalizada): {sede_id} - {sede_nombre_completo}")
            return sede_id, sede_nombre_completo, session_attributes, None

        # Si no hay coincidencia exacta, intenta buscar por similitud (difflib) sobre la lista de sedes de la ciudad
        matches = difflib.get_close_matches(sede_normalizada, sedes_nombres_normalizados, n=1, cutoff=0.7)
        if matches:
            idx = sedes_nombres_normalizados.index(matches[0])
            sede_real = sedes_nombres[idx]
            sede_id = obtener_id_sede(sede_real)
            sede_nombre_completo = obtener_nombre_sede_por_id(sede_id)
            if intent and "slots" in intent:
                intent["slots"]["sede"] = {
                    "value": {
                        "originalValue": sede_nombre_completo or sede_real,
                        "resolvedValues": [sede_nombre_completo or sede_real],
                        "interpretedValue": sede_nombre_completo or sede_real
                    },
                    "shape": "Scalar"
                }
            session_attributes["sede_id"] = str(sede_id)
            session_attributes["sede_nombre"] = sede_nombre_completo or sede_real.title()
            print(f"✅ Sede mapeada (difflib): {sede_id} - {sede_nombre_completo}")
            return sede_id, sede_nombre_completo, session_attributes, None
    
    if sede_id:
                    # Guardar el nombre COMPLETO de la sede, no el input original
                    sede_nombre_completo = obtener_nombre_sede_por_id(sede_id)
                    
                    # Actualizar el slot en el intent
                    if intent and "slots" in intent:
                        intent["slots"]["sede"] = {
                            "value": {
                                "originalValue": sede_nombre_completo or sede_raw,
                                "resolvedValues": [sede_nombre_completo or sede_raw],
                                "interpretedValue": sede_nombre_completo or sede_raw
                            },
                            "shape": "Scalar"
                        }
                    
                    session_attributes["sede_id"] = str(sede_id)
                    session_attributes["sede_nombre"] = sede_nombre_completo or sede_raw.title()
                    print(f"✅ Sede mapeada: {sede_id} - {sede_nombre_completo}")

                    return sede_id, sede_nombre_completo, session_attributes, None

    print("❌ Error inesperado en validar_sede_usuario")
    return None, None, session_attributes, None

def obtener_nombre_actividad_por_id(actividad_id):
    """Obtiene el nombre completo de la actividad dado su ID"""
    # Mapeo inverso basado en tu actividades_map existente
    actividades_id_nombre = {
        7: "Fitball", 24:"Fitball",
        10: "Pilates Reformer", 
        19: "Pilates Mat",
        25: "BodyBalance",
        29: "BodyPump",
        37: "G.A.P",
        38: "BodyCombat",
        40: "FitCombat",
        52: "CyclingTech Endurance",
        54: "CyclingTech Challenge", 
        57: "Bungee",
        59: "Funcional Cross",
        42: "Rumba",
        72: "Pilates Reformer Studio",
        113: "B Ride",
        114: "B Shape",
        21: "Stretching XP",
        26: "Barre",
        30: "Glúteo XP",
        31: "Glúteo",
        41: "Power Boxing",
        43: "Aeróbicos",
        45: "Step",
        109: "Core",
        110: "Aerologic",
        111: "Tono",
        112: "Sexy Dance",
        22: "Stretching",
        27: "CrossTech",
        28: "RIP 60",
        33: "Abdomen XP",
        35: "HIIT Grupal",
        39: "Tae Bo",
        44: "Danzika",
        46: "Danztep",
        47: "Zumba",
        127: "CyclingTech", 51: "Cyclintech",
        14: "Consulta Nutrición",
        20: "Yoga",
        32: "Abdomen",
        34: "Bootcamp",
        36: "Tabata X5",
        48: "Zumba Step",
        49: "BodyAttack",
        50: "Strong",
        53: "CyclingTech HIIT",
        55: "Sprint",
        61: "Danza Árabe",
        65: "Zonas Húmedas",
        69: "Hit Funcional",
        75: "Power Jump"
    }
    return actividades_id_nombre.get(actividad_id, "")

def obtener_id_actividad(nombre_actividad):
    print(f"🔍 ===== DEBUG OBTENER_ID_ACTIVIDAD =====")
    print(f"🔍 Input original: '{nombre_actividad}'")
    actividades_map = {
        "fitball": 7, "fitball": 24,
        "pilates reformer": 10,
        "pilates mat": 19,
        "bodybalance": 25,
        "bodypump": 29,
        "g.a.p": 37,
        "bodycombat": 38,
        "fitcombat": 40,
        "cyclingtech endurance": 52,
        "cyclingtech challenge": 54,
        "bungee": 57,
        "funcional cross": 59,
        "rumba": 42,
        "pilates reformer studio": 72,
        "b ride": 113,
        "b shape": 114,
        "stretching xp": 21,
        "barre": 26,
        "glúteo xp": 30,
        "glúteo": 31,
        "power boxing": 41,
        "aeróbicos": 43,
        "step": 45,
        "core": 109,
        "aerologic": 110,
        "tono": 111,
        "sexy dance": 112,
        "stretching": 22,
        "crosstech": 27,
        "rip 60": 28,
        "abdomen xp": 33,
        "hiit grupal": 35,
        "tae bo": 39,
        "danzika": 44,
        "danztep": 46,
        "zumba": 47,
        "cyclingtech": 127, "cyclingtech": 51,
        "consulta nutricion": 14,
        "yoga": 20,
        "abdomen": 32,
        "bootcamp": 34,
        "tabata x5": 36,
        "zumba step": 48,
        "bodyattack": 49,
        "strong": 50,
        "cyclingtech hiit": 53,
        "sprint": 55,
        "danza arabe": 61,
        "zonas humedas": 65,
        "hit funcional": 69,
        "power jump": 75,
        # Puedes agregar más variantes o sinónimos aquí si lo necesitas
    }
    nombre_normalizado = normalizar_nombre(nombre_actividad)
    print(f"🔍 Nombre normalizado: '{nombre_normalizado}'")
    
    actividades_map_normalizado = {normalizar_nombre(k): v for k, v in actividades_map.items()}
    print(f"🔍 Buscando en {len(actividades_map_normalizado)} actividades normalizadas")

    # 1. Búsqueda exacta
    print("🔍 1. Búsqueda exacta...")
    if nombre_normalizado in actividades_map_normalizado:
        print(f"✅ ENCONTRADO EXACTO: '{nombre_normalizado}'")
        return actividades_map_normalizado[nombre_normalizado]

    # 2. Coincidencia parcial mejorada
    print("🔍 2. Búsqueda parcial...")
    for k, v in actividades_map_normalizado.items():
        # A. El input está contenido en la actividad
        if len(nombre_normalizado) >= 3 and nombre_normalizado in k:
            print(f"✅ ENCONTRADO (input en actividad): '{nombre_normalizado}' in '{k}'")
            return v
        
        # B. La actividad está contenida en el input
        if len(k) >= 3 and k in nombre_normalizado:
            print(f"✅ ENCONTRADO (actividad en input): '{k}' in '{nombre_normalizado}'")
            return v
        
        # C. Prefijo del input
        if len(nombre_normalizado) >= 3 and k.startswith(nombre_normalizado):
            print(f"✅ ENCONTRADO (prefijo): '{k}'.startswith('{nombre_normalizado}')")
            return v
        abreviaciones_actividades = {
        # Basado en tu mapeo existente - solo abreviaciones naturales
        "gap": "g.a.p",
        "pump": "bodypump", 
        "combat": "bodycombat",
        "attack": "bodyattack",
        "balance": "bodybalance",
        "aero": "aeróbicos", 
        "aerobic": "aeróbicos",
        "abdom": "abdomen",
        "abs": "abdomen",
        "cross": "crosstech",
        "cycling": "cyclingtech",
        "hiit": "hiit grupal",
        "hit": "hiit grupal",
        "stretching": "stretching",
        "stretch": "stretching",
        "pilates": "pilates mat",
        "yoga": "yoga",
        "zumba": "zumba",
        "rumba": "rumba",
        "step": "step",
        "core": "core",
        "tono": "tono",
        "bootcamp": "bootcamp",
        "boot": "bootcamp",
        "strong": "strong",
        "barre": "barre",
        "sprint": "sprint",
        "zonas": "zonas humedas",
        "zonas húmedas": "zonas humedas",
        "vapor": "zonas humedas",
        "sauna": "zonas humedas",
        # Solo abreviaciones naturales de lo que YA tienes
    }
        
    for abrev, actividad_completa in abreviaciones_actividades.items():
        if nombre_normalizado == abrev or abrev in nombre_normalizado:
            if actividad_completa in actividades_map_normalizado:
                print(f"✅ ENCONTRADO (abreviación): '{abrev}' → '{actividad_completa}'")
                return actividades_map_normalizado[actividad_completa]

    # 4. Búsqueda con difflib (cutoff dinámico)
    cutoff_dinamico = 0.4 if len(nombre_normalizado) >= 5 else 0.3
    matches = difflib.get_close_matches(nombre_normalizado, actividades_map_normalizado.keys(), n=1, cutoff=cutoff_dinamico)
    if matches:
        print(f"✅ Actividad corregida (difflib): '{nombre_actividad}' → '{matches[0]}'")
        return actividades_map_normalizado[matches[0]]

    print("❌ NO ENCONTRADO")
    return None


def validar_clase_usuario(slots, session_attributes, input_transcript, intent, sede_id):
    print("🔍 ===== DEBUG VALIDAR CLASE =====")
    print("🔍 Slots recibidos:", slots)
    print("🔍 input_transcript:", input_transcript)
    print("🔍 sede_id:", sede_id)
    
    clase_raw = ""

    # 1. Intenta extraer de los slots
    if slots:
        clase_slot = slots.get("clase")
        print("🔍 clase_slot:", clase_slot)
        if clase_slot and isinstance(clase_slot, dict):
            clase_raw = (
                clase_slot.get("value", {}).get("interpretedValue")
                or clase_slot.get("value", {}).get("originalValue")
                or ""
            )
            print("🔍 clase_raw extraído de slots:", clase_raw)

    # 2. Si el slot está vacío, usa el input_transcript
    if not clase_raw and input_transcript:
        clase_raw = input_transcript.strip().lower()
        print("🔍 clase_raw extraído de input_transcript:", clase_raw)

    print("📌 clase_raw FINAL:", clase_raw)
    
    if not clase_raw:
        print("❌ clase_raw está vacío, retornando None")
        return None, None, session_attributes, None
    
    # 3. Normalizar nombre y obtener ID de actividad
    clase_normalizada = normalizar_nombre(clase_raw)
    print("🔍 clase_normalizada:", clase_normalizada)
    
    clase_id = obtener_id_actividad(clase_normalizada)
    print("🔍 clase_id obtenido:", clase_id)
    
    if not clase_id:
        # Obtener lista de clases disponibles para la sede
        clases_disponibles = consultar_clases_por_sede_id(sede_id)
        clases_nombres = [c['clase'] for c in clases_disponibles] if clases_disponibles else []
        
        mensaje = f"No reconozco la clase '{clase_raw}'. Las clases disponibles en esta sede son: {', '.join(clases_nombres)}"
        respuesta = {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "clase"},
                "intent": {
                    "name": intent.get("name"),
                    "slots": intent.get("slots", {}),
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{"contentType": "PlainText", "content": mensaje}]
        }
        return None, None, session_attributes, respuesta

    # 4. Obtener nombre completo de la clase
    clase_nombre_completo = obtener_nombre_actividad_por_id(clase_id)
    
    # Actualizar el slot si se encontró una coincidencia aproximada
    if clase_nombre_completo and clase_nombre_completo.lower() != clase_raw.lower():
        print(f"🔄 CORRECCIÓN: '{clase_raw}' → '{clase_nombre_completo}'")
        
        # Actualizar el slot con el nombre correcto
        if "clase" in intent.get("slots", {}):
            intent["slots"]["clase"] = {
                "value": {
                    "originalValue": clase_nombre_completo,
                    "resolvedValues": [clase_nombre_completo],
                    "interpretedValue": clase_nombre_completo
                },
                "shape": "Scalar"
            }
            print(f"✅ Slot 'clase' actualizado con '{clase_nombre_completo}'")
    
    # 5. Guardar en sesión
    session_attributes["clase_id"] = str(clase_id)
    session_attributes["clase_nombre"] = clase_nombre_completo or clase_raw.title()
    print(f"✅ Clase mapeada: {clase_id} - {clase_nombre_completo}")

    return clase_id, clase_nombre_completo, session_attributes, None

def consultar_sedes_por_ciudad_id(ciudad_id):
    ciudad_sedes_map = {
        1: [
            "calle 90", "cabrera", "autopista 135", "fontanar", "plaza bosa", "torre central",
            "carrera 11", "titan plaza", "floresta", "country 138", "country", "gran estacion", "paseo del rio",
            "autopista 170", "portal 80", "diverplaza", "centro mayor", "chico", "galerias",
            "santa ana", "pasadena", "kennedy", "suba", "hayuelos", "normandia", "colina",
            "sultana", "plaza central", "chapinero", "cedritos", "ensueño", "pablo vi", "bulevar",
            "calle 122 studio", "connecta"
        ],  # Bogotá
        2: [
            "belen", "santa maria de los angeles (vegas)", "san lucas", "laureles", "vizcaya", "city plaza",
            "avenida colombia", "mall del este", "villagrande", "premium plaza", "camino real",
            "san juan", "robledo", "llanogrande", "americas"
        ],  # Medellín
        3: ["terreros", "antares"],  # Soacha
        4: ["viva villavicencio", "llanocentro"],  # Villavicencio
        5: ["recreo", "parque washington", "viva barranquilla", "miramar"],  # Barranquilla
        6: ["armenia"],  # Armenia
        7: ["tulua"],  # Tuluá
        8: ["gran manzana", "caribe plaza", "plazuela", "ejecutivos", "bocagrande"],  # Cartagena
        9: ["caracoli", "megamall", "cacique"],  # Bucaramanga
        10: ["caney", "chipichape", "jardin plaza", "oeste"],  # Cali
        11: ["niquia"],  # Bello
        12: ["niquia"],  # Bello (si aplica, puedes ajustar)
        13: [],  # Neiva (sin sedes especificadas)
        14: ["palmira"],  # Palmira
        15: [],  # Valledupar (sin sedes especificadas)
        16: ["manizales"],  # Manizales
        18: [],  # Envigado (sin sedes especificadas)
        19: ["ibague"],  # Ibagué
        20: ["chia"],  # Chía
        21: ["dos quebradas"],  # Dosquebradas
        22: ["cucuta"],  # Cúcuta
        23: ["pasto"],  # Pasto
        24: ["pereira", "dos quebradas"],  # Pereira
        36: ["tunja"],  # Tunja
    }
    return [{"sede_nombre": s} for s in ciudad_sedes_map.get(ciudad_id, [])]

def get_slot_value(slots, slot_name):
    slot = slots.get(slot_name)
    if slot and isinstance(slot, dict):
        value = slot.get("value", {})
        return value.get("interpretedValue") or value.get("originalValue") or ""
    return ""

CATEGORIAS_SEDES = [
    {"id": 5, "nombre": "Classic", "brand_id": 1},
    {"id": 15, "nombre": "One Plus", "brand_id": 1},
    {"id": 12, "nombre": "Athletic", "brand_id": 2},
    {"id": 13, "nombre": "Corporativo", "brand_id": 1},
    {"id": 6, "nombre": "Super", "brand_id": 1},
    {"id": 14, "nombre": "Pilates Studio", "brand_id": 1},
    {"id": 1, "nombre": "Platino", "brand_id": 1},
    {"id": 2, "nombre": "One", "brand_id": 1},
    {"id": 4, "nombre": "Premium", "brand_id": 1},
]

def obtener_categorias_por_linea(linea):
    """
    Retorna la lista de nombres de categorías para la línea dada.
    línea: "bodytech" o "athletic"
    """
    linea = linea.strip().lower()
    brand_id = 1 if linea == "bodytech" else 2
    return [cat["nombre"] for cat in CATEGORIAS_SEDES if cat["brand_id"] == brand_id]

################
# Normalizar Nombre Sede
################

def normalizar_ciudad(ciudad_raw):
    # Toma solo la primera variante antes de la coma
    ciudad = ciudad_raw.split(",")[0].strip().lower()
    # Quita tildes
    ciudad = ''.join(
        c for c in unicodedata.normalize('NFD', ciudad)
        if unicodedata.category(c) != 'Mn'
    )
    return ciudad

def obtener_id_categoria_por_nombre(nombre_categoria, brand_id):
    nombre_categoria = normalizar_nombre(nombre_categoria)
    for cat in CATEGORIAS_SEDES:
        if normalizar_nombre(cat["nombre"]) == nombre_categoria and cat["brand_id"] == brand_id:
            return cat["id"]
    return None

################
# forzar Flujo de ciudad solo para consultas requeridas
################


def validar_y_forzar_flujo_ciudad(intent_name, slots, session_attributes, input_transcript, intent, flujo_grupales_por_ciudad):
    """
    Centraliza la validación para forzar el flujo por ciudad si el input parece una ciudad válida.
    Retorna la respuesta del flujo forzado si aplica, o None si no aplica.
    """
    if intent_name in ["ConsultaGrupales", "ConsultarSedes"]:
        ciudad_raw = slots.get("ciudad", {}).get("value", {}).get("interpretedValue", "")
        if not ciudad_raw or ciudad_raw in ["ciudad", ""]:
            # Solo si el input parece una ciudad (máximo 2 palabras y no contiene números)
            if len(input_transcript.split()) <= 2 and not any(char.isdigit() for char in input_transcript):
                ciudad_id, ciudad_nombre, _, _ = validar_ciudad_usuario(
                    slots, session_attributes, input_transcript, intent
                )
                if ciudad_id:
                    return flujo_grupales_por_ciudad(ciudad_id, ciudad_nombre)
    return None

def flujo_grupales_por_ciudad(ciudad_id, ciudad_nombre, session_attributes=None):
    """
    Prepara y retorna la respuesta para iniciar el flujo ConsultaGrupales con la ciudad indicada.
    """
    if session_attributes is None:
        session_attributes = {}

    # Limpiar atributos que puedan bloquear el flujo
    for key in [
        "sede_nombre", "sede_id", "clase_display", "slots_previos",
        "esperando_transicion_grupales", "en_flujo_activo"
    ]:
        session_attributes.pop(key, None)
    session_attributes["ciudad_id"] = str(ciudad_id)
    session_attributes["ciudad_nombre"] = ciudad_nombre

    slots = {
        "ciudad": {
            "value": {
                "originalValue": ciudad_nombre,
                "resolvedValues": [ciudad_nombre],
                "interpretedValue": ciudad_nombre
            },
            "shape": "Scalar"
        }
    }

    return {
        "sessionState": {
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": "sede"},
            "intent": {
                "name": "ConsultaGrupales",
                "slots": slots,
                "state": "InProgress"
            },
            "sessionAttributes": session_attributes
        },
        "messages": [{
            "contentType": "PlainText",
            "content": f"¿En qué sede de {ciudad_nombre} deseas consultar actividades grupales?"
        }]
    }

def obtener_ciudades_validas():

    return [
        "Bogotá", "Medellín", "Soacha", "Villavicencio", "Barranquilla", "Armenia", "Tuluá",
        "Cartagena", "Bucaramanga", "Cali", "Montería", "Bello", "Neiva", "Palmira", "Valledupar",
        "Manizales", "Envigado", "Ibagué", "Chía", "Dosquebradas", "Cúcuta", "Pasto", "Pereira", "Tunja"
    ]

def obtener_sedes_validas():

    sedes = set()
    from services import consultar_sedes_por_ciudad_id
    for ciudad_id in range(1, 37):
        for s in consultar_sedes_por_ciudad_id(ciudad_id):
            sedes.add(s['sede_nombre'].title())
    return sorted(sedes)
    
def obtener_clases_validas():

    actividades_map = get_actividades_map_normalizado()
    return sorted(set(nombre.title() for nombre in actividades_map.keys()))


def detectar_tipo_input_inteligente(input_transcript):
    """
    Detecta inteligentemente qué tipo de información contiene el input del usuario.
    Retorna un diccionario con los elementos detectados y su nivel de confianza.
    
    ORDEN DE PRIORIDAD:
    1. FECHA (máxima prioridad - son únicas y específicas)
    2. CIUDAD (segunda prioridad - contexto geográfico principal)
    3. SEDE (tercera prioridad - más específica que ciudad)
    4. CLASE (cuarta prioridad - requiere contexto de sede)
    """
    if not input_transcript or not input_transcript.strip():
        return {"tipo": None, "valor": None, "confianza": 0}
    
    input_limpio = input_transcript.strip().lower()
    resultados = {"detecciones": [], "principal": None}
    
    print(f"🔍 ===== DETECCIÓN INTELIGENTE =====")
    print(f"🔍 Analizando: '{input_transcript}'")
    
    palabras_genericas = {
        "clases", "clase", "actividades", "actividad", "grupales", "grupal",
        "horarios", "horario", "ejercicios", "ejercicio", "entrenamientos", 
        "entrenamiento", "deportes", "deporte", "fitness", "gimnasio",
        "sede", "sedes", "ciudad", "ciudades", "donde", "que", "hay",
        "consultar", "consulta", "ver", "mostrar", "informacion", "info",
        "hola", "buenos", "dias", "tardes", "noches", "saludos", "ayuda",
        "gracias", "por", "favor", "quiero", "necesito", "deseo"
    }
    #  Si es palabra genérica, retornar "desconocido"**
    if input_limpio in palabras_genericas:
        print(f"🚫 '{input_limpio}' es una palabra genérica, NO procesable como entidad específica")
        resultados["principal"] = "generico"
        return resultados
    
    # 1. DETECTAR FECHA (máxima prioridad)
    print("🔍 1. Verificando FECHA...")
    fecha_normalizada, _ = normalizar_fecha(input_limpio)
    if fecha_normalizada:
        print(f"✅ FECHA detectada: '{fecha_normalizada}' (confianza: 100%)")
        resultados["detecciones"].append({
            "tipo": "fecha",
            "valor": fecha_normalizada,
            "original": input_transcript,
            "confianza": 100
        })
        resultados["principal"] = "fecha"
        return resultados
    
    # 2. DETECTAR CIUDAD (segunda prioridad)
    print("🔍 2. Verificando CIUDAD...")
    ciudades_map = {
        "bogota": 1, "bogotá": 1, "bogote": 1, "bogoto": 1, "bogito": 1, "bogti": 1,
        "medellin": 2, "medellín": 2, "mede": 2, "medelin": 2, "medelein": 2, "medallo": 2,
        "soacha": 3, "socha": 3, "socaha": 3,
        "villavicencio": 4, "villa": 4, "villavi": 4, "villavicenci": 4, "villavicensio": 4,
        "barranquilla": 5, "barran": 5, "barranqui": 5, "barranquila": 5, "barraquilla": 5,
        "armenia": 6, "armeni": 6, "arminia": 6,
        "tulua": 7, "tuluá": 7, "tulu": 7, "tulú": 7,
        "cartagena": 8, "cartage": 8, "cartagina": 8,
        "bucaramanga": 9, "bucara": 9, "bucaraman": 9, "bucaramang": 9, "bucarmanga": 9,
        "cali": 10, "cal": 10, "calli": 10,
        "monteria": 11, "montería": 11, "monte": 11, "monteri": 11, "montria": 11,
        "bello": 12, "belo": 12, "bell": 12,
        "neiva": 13, "neiv": 13, "neva": 13,
        "palmira": 14, "palmi": 14, "palmyra": 14,
        "valledupar": 15, "valle": 15, "valledup": 15, "valedupa": 15,
        "manizales": 16, "mani": 16, "manizale": 16, "manizalés": 16,
        "envigado": 18, "envi": 18, "enviga": 18,
        "ibague": 19, "ibagué": 19, "ibag": 19, "ibage": 19,
        "chia": 20, "chía": 20, "chi": 20,
        "dosquebradas": 21, "dos": 21, "dosque": 21, "quebradas": 21,
        "cucuta": 22, "cúcuta": 22, "cucu": 22, "cucta": 22,
        "pasto": 23, "past": 23,
        "pereira": 24, "perei": 24, "perira": 24, "pereria": 24,
        "tunja": 36, "tunj": 36, "tnja": 36
    }
    
    ciudades_id_nombre = {
        1: "Bogotá", 2: "Medellín", 3: "Soacha", 4: "Villavicencio", 5: "Barranquilla",
        6: "Armenia", 7: "Tuluá", 8: "Cartagena", 9: "Bucaramanga", 10: "Cali",
        11: "Montería", 12: "Bello", 13: "Neiva", 14: "Palmira", 15: "Valledupar",
        16: "Manizales", 18: "Envigado", 19: "Ibagué", 20: "Chía", 21: "Dosquebradas",
        22: "Cúcuta", 23: "Pasto", 24: "Pereira", 36: "Tunja"
    }
    
    # Detección exacta de ciudades
    input_normalizado = normalizar_nombre(input_limpio)
    for ciudad_key, ciudad_id in ciudades_map.items():
        if input_normalizado == normalizar_nombre(ciudad_key):
            ciudad_nombre = ciudades_id_nombre[ciudad_id]
            print(f"✅ CIUDAD detectada (exacta): '{ciudad_nombre}' (confianza: 95%)")
            resultados["detecciones"].append({
                "tipo": "ciudad",
                "valor": ciudad_nombre,
                "id": ciudad_id,
                "original": input_transcript,
                "confianza": 95
            })
            resultados["principal"] = "ciudad"
            break
    
    # Si no se detectó ciudad exacta, usar difflib pero con confianza menor
    if not resultados["detecciones"]:
        import difflib
        todas_ciudades = list(ciudades_map.keys())
        matches = difflib.get_close_matches(input_normalizado, todas_ciudades, n=1, cutoff=0.7)
        if matches:
            ciudad_id = ciudades_map[matches[0]]
            ciudad_nombre = ciudades_id_nombre[ciudad_id]
            print(f"✅ CIUDAD detectada (aproximada): '{ciudad_nombre}' (confianza: 80%)")
            resultados["detecciones"].append({
                "tipo": "ciudad",
                "valor": ciudad_nombre,
                "id": ciudad_id,
                "original": input_transcript,
                "confianza": 80
            })
            resultados["principal"] = "ciudad"
    
    # 3. DETECTAR SEDE (solo si NO es una ciudad con alta confianza)
    if (not resultados["detecciones"] or 
        (resultados["detecciones"] and resultados["detecciones"][0]["confianza"] < 85)):
        
        print("🔍 3. Verificando SEDE...")
        
        # **PROTECCIÓN ADICIONAL: Verificar que no sea palabra genérica antes de buscar sede**
        if input_normalizado in [normalizar_nombre(p) for p in palabras_genericas]:
            print(f"🚫 '{input_limpio}' es palabra genérica, NO buscar como sede")
        else:
            sede_id = obtener_id_sede_mejorado(input_normalizado, palabras_genericas)
            if sede_id:
                sede_nombre = obtener_nombre_sede_por_id(sede_id)
                # Verificar que no sea una ciudad mal interpretada
                if sede_nombre and input_normalizado not in [normalizar_nombre(c) for c in ciudades_map.keys()]:
                    print(f"✅ SEDE detectada: '{sede_nombre}' (confianza: 75%)")
                    # Si ya había una ciudad con baja confianza, reemplazar
                    if resultados["detecciones"] and resultados["detecciones"][0]["confianza"] < 85:
                        resultados["detecciones"] = []
                    resultados["detecciones"].append({
                        "tipo": "sede",
                        "valor": sede_nombre,
                        "id": sede_id,
                        "original": input_transcript,
                        "confianza": 75
                    })
                    resultados["principal"] = "sede"
    
    # 4. DETECTAR CLASE (solo si ya tenemos contexto o es muy específica)
    if not resultados["detecciones"]:
        print("🔍 4. Verificando CLASE...")
        
        # **PROTECCIÓN ADICIONAL: No buscar clases si es palabra genérica**
        if input_normalizado in [normalizar_nombre(p) for p in palabras_genericas]:
            print(f"🚫 '{input_limpio}' es palabra genérica, NO buscar como clase")
        else:
            clase_id = obtener_id_actividad(input_normalizado)
            if clase_id:
                clase_nombre = obtener_nombre_actividad_por_id(clase_id)
                if clase_nombre:
                    print(f"✅ CLASE detectada: '{clase_nombre}' (confianza: 70%)")
                    resultados["detecciones"].append({
                        "tipo": "clase",
                        "valor": clase_nombre,
                        "id": clase_id,
                        "original": input_transcript,
                        "confianza": 70
                    })
                    resultados["principal"] = "clase"
    
    # Si no se detectó nada
    if not resultados["detecciones"]:
        print("❌ No se detectó ningún tipo específico")
        resultados["principal"] = "desconocido"
    
    print(f"🔍 ===== RESULTADO FINAL =====")
    print(f"🔍 Principal: {resultados['principal']}")
    print(f"🔍 Detecciones: {resultados['detecciones']}")
    print(f"🔍 ==============================")
    
    return resultados

def obtener_ciudad_fallback_por_sede(sede_id):
    """
    Función de fallback que proporciona mapeo hardcodeado sede -> ciudad
    para las sedes más comunes cuando no se puede acceder a la base de datos
    """
    fallback_map = {
        # Bogotá (ciudad_id: 1)
        2: {"id": 1, "nombre": "Bogotá"},      # Chico
        3: {"id": 1, "nombre": "Bogotá"},      # Kennedy  
        6: {"id": 1, "nombre": "Bogotá"},      # Carrera 11
        7: {"id": 1, "nombre": "Bogotá"},      # Normandia
        8: {"id": 1, "nombre": "Bogotá"},      # Centro Mayor
        10: {"id": 1, "nombre": "Bogotá"},     # Autopista 135
        11: {"id": 1, "nombre": "Bogotá"},     # Autopista 170
        17: {"id": 1, "nombre": "Bogotá"},     # Cabrera
        20: {"id": 1, "nombre": "Bogotá"},     # Cedritos
        21: {"id": 1, "nombre": "Bogotá"},     # Chapinero
        22: {"id": 1, "nombre": "Bogotá"},     # Chia
        24: {"id": 1, "nombre": "Bogotá"},     # Colina
        27: {"id": 1, "nombre": "Bogotá"},     # Hayuelos
        34: {"id": 1, "nombre": "Bogotá"},     # Pablo VI
        35: {"id": 1, "nombre": "Bogotá"},     # Pasadena
        37: {"id": 1, "nombre": "Bogotá"},     # Portal 80
        39: {"id": 1, "nombre": "Bogotá"},     # Suba
        40: {"id": 1, "nombre": "Bogotá"},     # Sultana
        41: {"id": 1, "nombre": "Bogotá"},     # Torre Central
        99: {"id": 1, "nombre": "Bogotá"},     # Titan Plaza
        102: {"id": 1, "nombre": "Bogotá"},    # Bulevar
        103: {"id": 1, "nombre": "Bogotá"},    # Plaza Central
        105: {"id": 1, "nombre": "Bogotá"},    # Antares
        106: {"id": 1, "nombre": "Bogotá"},    # Diverplaza
        110: {"id": 1, "nombre": "Bogotá"},    # Galerias
        111: {"id": 1, "nombre": "Bogotá"},    # Fontanar
        115: {"id": 1, "nombre": "Bogotá"},    # Plaza Bosa
        116: {"id": 1, "nombre": "Bogotá"},    # Country 138
        117: {"id": 1, "nombre": "Bogotá"},    # Terreros
        122: {"id": 1, "nombre": "Bogotá"},    # Calle 90
        123: {"id": 1, "nombre": "Bogotá"},    # Ensueño
        126: {"id": 1, "nombre": "Bogotá"},    # Paseo del Rio
        127: {"id": 1, "nombre": "Bogotá"},    # Santa Ana
        151: {"id": 1, "nombre": "Bogotá"},    # Connecta
        152: {"id": 1, "nombre": "Bogotá"},    # Calle 122 Studio
        
        # Medellín (ciudad_id: 2)
        44: {"id": 2, "nombre": "Medellín"},   # Laureles
        45: {"id": 2, "nombre": "Medellín"},   # Premium Plaza
        47: {"id": 2, "nombre": "Medellín"},   # Vizcaya
        48: {"id": 2, "nombre": "Medellín"},   # Santa Maria de los Angeles (Vegas)
        55: {"id": 2, "nombre": "Medellín"},   # Robledo
        58: {"id": 2, "nombre": "Medellín"},   # Avenida Colombia
        59: {"id": 2, "nombre": "Medellín"},   # City Plaza
        66: {"id": 2, "nombre": "Medellín"},   # San Lucas
        69: {"id": 2, "nombre": "Medellín"},   # Villagrande
        75: {"id": 2, "nombre": "Medellín"},   # Americas
        76: {"id": 2, "nombre": "Medellín"},   # Belen
        77: {"id": 2, "nombre": "Medellín"},   # Camino Real
        80: {"id": 2, "nombre": "Medellín"},   # San Juan AGREGADO
        81: {"id": 2, "nombre": "Medellín"},   # Mall del Este
        125: {"id": 2, "nombre": "Medellín"},  # Llanogrande
        
        # Bello (ciudad_id: 12)
        57: {"id": 12, "nombre": "Bello"},     # Niquia
        
        # Envigado (ciudad_id: 18)
        # Las sedes de Envigado están mapeadas como ciudad separada
        
        # Cali (ciudad_id: 10)
        23: {"id": 10, "nombre": "Cali"},      # Chipichape
        29: {"id": 10, "nombre": "Cali"},      # Jardin Plaza
        53: {"id": 10, "nombre": "Cali"},      # Floresta
        71: {"id": 10, "nombre": "Cali"},      # Oeste
        94: {"id": 10, "nombre": "Cali"},      # Caney
        
        # Barranquilla (ciudad_id: 5)
        32: {"id": 5, "nombre": "Barranquilla"}, # Miramar
        56: {"id": 5, "nombre": "Barranquilla"}, # Recreo
        101: {"id": 5, "nombre": "Barranquilla"}, # Viva Barranquilla
        112: {"id": 5, "nombre": "Barranquilla"}, # Parque Washington
        
        # Bucaramanga (ciudad_id: 9)
        15: {"id": 9, "nombre": "Bucaramanga"}, # Megamall
        63: {"id": 9, "nombre": "Bucaramanga"}, # Caracoli
        85: {"id": 9, "nombre": "Bucaramanga"}, # Cacique
        
        # Cartagena (ciudad_id: 8)
        19: {"id": 8, "nombre": "Cartagena"},   # Caribe Plaza
        36: {"id": 8, "nombre": "Cartagena"},   # Plazuela
        86: {"id": 8, "nombre": "Cartagena"},   # Bocagrande
        90: {"id": 8, "nombre": "Cartagena"},   # Ejecutivos
        185: {"id": 8, "nombre": "Cartagena"},  # Gran Manzana
        
        # Pereira (ciudad_id: 24)
        50: {"id": 24, "nombre": "Pereira"},    # Pereira
        68: {"id": 21, "nombre": "Dosquebradas"}, # Dos Quebradas (ciudad separada)
        
        # Armenia (ciudad_id: 6)
        70: {"id": 6, "nombre": "Armenia"},     # Armenia
        
        # Manizales (ciudad_id: 16)
        46: {"id": 16, "nombre": "Manizales"},  # Manizales
        
        # Ibagué (ciudad_id: 19)
        72: {"id": 19, "nombre": "Ibagué"},     # Ibague
        
        # Villavicencio (ciudad_id: 4)
        43: {"id": 4, "nombre": "Villavicencio"}, # Llanocentro
        92: {"id": 4, "nombre": "Villavicencio"}, # Viva Villavicencio
        
        # Cúcuta (ciudad_id: 22)
        25: {"id": 22, "nombre": "Cúcuta"},     # Cucuta
        
        # Pasto (ciudad_id: 23)
        87: {"id": 23, "nombre": "Pasto"},      # Pasto
        
        # Tuluá (ciudad_id: 7)
        91: {"id": 7, "nombre": "Tuluá"},       # Tulua
        
        # Palmira (ciudad_id: 14)
        96: {"id": 14, "nombre": "Palmira"},    # Palmira
        
        # Tunja (ciudad_id: 36)
        150: {"id": 36, "nombre": "Tunja"},     # Tunja
    }
    
    return fallback_map.get(int(sede_id), None)

def obtener_id_sede_mejorado(nombre_sede, palabras_genericas):
    """
    Versión mejorada de obtener_id_sede que evita confundir palabras genéricas con sedes.
    """
    # Verificar si es palabra genérica
    nombre_normalizado = normalizar_nombre(nombre_sede)
    if nombre_normalizado in [normalizar_nombre(p) for p in palabras_genericas]:
        print(f"🚫 '{nombre_sede}' es palabra genérica, NO buscar como sede")
        return None
    
    # **CUTOFF MÁS ALTO para difflib para evitar falsos positivos**
    sede_id = obtener_id_sede(nombre_sede)
    if sede_id:
        # **VERIFICACIÓN ADICIONAL: Si el input es muy genérico, rechazar el resultado**
        if len(nombre_normalizado) <= 4 and nombre_normalizado in ["clas", "clase", "activ", "grup"]:
            print(f"🚫 Input '{nombre_sede}' demasiado genérico para ser sede")
            return None
    
    return sede_id

def extraer_parametros_con_bedrock(input_transcript):
    """
    Usa Bedrock para extraer parámetros de consultas complejas de clases grupales.
    """
    try:
        prompt = f"""
Analiza el siguiente texto y extrae información sobre consultas de clases grupales:

Texto: "{input_transcript}"

Extrae y devuelve SOLO un JSON con esta estructura exacta:
{{
    "ciudad": "nombre_ciudad_si_se_menciona_o_null",
    "sede": "nombre_sede_si_se_menciona_o_null", 
    "clase": "nombre_clase_si_se_menciona_o_null",
    "fecha": "fecha_en_formato_YYYY-MM-DD_si_se_menciona_o_null"
}}

Reglas:
- Para ciudades: Bogotá, Medellín, Cali, etc.
- Para sedes: chico, centro mayor, normandia, poblado, laureles, etc.
- Para clases: yoga, pilates, zumba, spinning, aqua, funcional, crossfit, bodypump, bodycombat, bodybalance, bodyattack, rumba, danza, boxeo, kickboxing, tabata, hiit, gap, abdomen, glúteos, stretching, power, strong, bootcamp, aeróbicos, step, core, barre, etc.
- Para fechas: convierte "hoy", "mañana", fechas relativas a formato YYYY-MM-DD
- Si no detectas algún parámetro, usa null
- NO agregues explicaciones, solo el JSON

**IMPORTANTE**: "rumba" es una clase de baile/danza muy común, siempre detectarla.

Responde únicamente con el JSON válido:
"""
        
        config = obtener_secret("main/LexAgenteVirtualSAC")
        respuesta = consultar_bedrock_generacion(prompt)
        
        print(f"🧠 Respuesta cruda de Bedrock: {respuesta}")
        
        # Intentar extraer JSON de la respuesta
        import json
        try:
            # Buscar JSON en la respuesta
            start_idx = respuesta.find('{')
            end_idx = respuesta.rfind('}') + 1
            if start_idx >= 0 and end_idx > start_idx:
                json_str = respuesta[start_idx:end_idx]
                resultado = json.loads(json_str)
                
                # Validar estructura
                if isinstance(resultado, dict):
                    # Limpiar valores null/vacíos
                    resultado_limpio = {}
                    for key, value in resultado.items():
                        if value and value.lower() not in ['null', 'none', '']:
                            resultado_limpio[key] = value.strip()
                    
                    print(f"🧠 JSON extraído y limpiado: {resultado_limpio}")
                    return resultado_limpio
                    
        except json.JSONDecodeError as e:
            print(f"❌ Error parseando JSON de Bedrock: {e}")
            
    except Exception as e:
        print(f"❌ Error en extracción con Bedrock: {e}")
    
    return None

def obtener_sedes_compuestas():
    """Retorna el mapeo de sedes compuestas"""
    return {
        "centro mayor": {"id": 8, "nombre": "Centro Mayor"},
        "calle 90": {"id": 122, "nombre": "Calle 90"},
        "autopista 170": {"id": 11, "nombre": "Autopista 170"},
        "autopista 135": {"id": 10, "nombre": "Autopista 135"},
        "gran estacion": {"id": 107, "nombre": "Gran Estacion"},
        "torre central": {"id": 41, "nombre": "Torre Central"},
        "carrera 11": {"id": 6, "nombre": "Carrera 11"},
        "plaza central": {"id": 103, "nombre": "Plaza Central"},
        "pablo vi": {"id": 34, "nombre": "Pablo VI"},
        "country 138": {"id": 116, "nombre": "Country 138"},
        "country": {"id": 116, "nombre": "Country 138"},
        "country club": {"id": 116, "nombre": "Country 138"},
        "portal 80": {"id": 37, "nombre": "Portal 80"},
        "plaza bosa": {"id": 115, "nombre": "Plaza Bosa"},
        "paseo del rio": {"id": 126, "nombre": "Paseo del Rio"},
        "santa ana": {"id": 127, "nombre": "Santa Ana"},
        "city plaza": {"id": 59, "nombre": "City Plaza"},
        "jardin plaza": {"id": 29, "nombre": "Jardin Plaza"},
        "viva barranquilla": {"id": 101, "nombre": "Viva Barranquilla"},
        "parque washington": {"id": 112, "nombre": "Parque Washington"},
        "caribe plaza": {"id": 19, "nombre": "Caribe Plaza"},
        "calle 122 studio": {"id": 152, "nombre": "Calle 122 Studio"},
        "gran manzana": {"id": 185, "nombre": "Gran Manzana"},
        "dos quebradas": {"id": 68, "nombre": "Dos Quebradas"},
        "viva villavicencio": {"id": 92, "nombre": "Viva Villavicencio"},
        "titan plaza": {"id": 99, "nombre": "Titan Plaza"},
        "premium plaza": {"id": 45, "nombre": "Premium Plaza"},
        "mall del este": {"id": 81, "nombre": "Mall del Este"},
        "santa maria de los angeles": {"id": 48, "nombre": "Santa Maria de los Angeles"},
        "avenida colombia": {"id": 58, "nombre": "Avenida Colombia"},
        "camino real": {"id": 77, "nombre": "Camino Real"},
        "san lucas": {"id": 66, "nombre": "San Lucas"},
        "san juan": {"id": 80, "nombre": "San Juan"}
    }

def extraer_y_validar_slots_grupales(input_transcript, session_attributes, intent):
    """
    Extrae y valida parámetros para ConsultaGrupales desde texto libre.
    NUEVA FUNCIONALIDAD: Maneja clases + sedes de otras ciudades mejor
    """
    print(f"🔍 === INICIO extraer_y_validar_slots_grupales ===")
    print(f"🔍 Input: '{input_transcript}'")
    print(f"🔍 Session attributes: {session_attributes}")
    
    # 🆕 VERIFICACIÓN ESPECIAL PARA CENTRO MAYOR
    if "centro mayor" in input_transcript.lower():
        print("🎯 DETECTADO 'centro mayor' en input - activando búsqueda de sedes compuestas")
    
    slots = intent.get("slots", {})
    
    # Variables de extracción
    ciudad_id = session_attributes.get("ciudad_id")
    ciudad = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
    sede_id = session_attributes.get("sede_id")
    sede_nombre = session_attributes.get("sede_nombre")
    clase_id = session_attributes.get("clase_id")
    clase_nombre = session_attributes.get("clase_nombre")
    fecha = None  # ✅ INICIALIZAR FECHA COMO None
    
    print(f"🔍 Variables de sesión - ciudad_id: {ciudad_id}, sede_id: {sede_id}, clase_id: {clase_id}")
    
    # ✅ PALABRAS GENÉRICAS QUE NO DEBEN SER INTERPRETADAS COMO ENTIDADES
    palabras_genericas = {
        "que", "clases", "clase", "actividades", "actividad", "grupales", "grupal",
        "horarios", "horario", "ejercicios", "ejercicio", "entrenamientos", 
        "entrenamiento", "deportes", "deporte", "fitness", "gimnasio",
        "sede", "sedes", "ciudad", "ciudades", "donde", "hay", "tienen",
        "consultar", "consulta", "ver", "mostrar", "informacion", "info",
        "hola", "buenos", "dias", "tardes", "noches", "saludos", "ayuda",
        "gracias", "por", "favor", "quiero", "necesito", "deseo", "como",
        "cuando", "para", "con", "sin", "hasta", "desde", "sobre", "entre"
    }
    
    # ✅ EXTRACCIÓN MEJORADA CON FILTRO DE PALABRAS GENÉRICAS
    input_lower = input_transcript.lower()
    palabras = input_lower.split()
    
    # PASO 1: EXTRAER FECHA PRIMERO (máxima prioridad)
    fecha = session_attributes.get("fecha_temporal") or session_attributes.get("fecha")  # Recuperar fecha temporal o guardada
    fecha_slot = get_slot_value(slots, "fecha") or get_slot_value(slots, "Fecha")
    if fecha_slot:
        fecha = fecha_slot
        print(f"✅ Fecha extraída de slots: {fecha}")
    elif input_transcript and not fecha:
        # Buscar patrones de fecha en el input
        import re
        from datetime import datetime, timedelta
        
        input_lower = input_transcript.lower()
        
        # Patrones de fecha comunes
        patron_fecha = r'\b(\d{1,2})\s*de\s*(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b'
        patron_fecha_numero = r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b'
        patron_fecha_guion = r'\b(\d{4})-(\d{1,2})-(\d{1,2})\b'
        
        # Buscar fecha con patrón "30 de julio"
        match_fecha = re.search(patron_fecha, input_lower)
        if match_fecha:
            dia = match_fecha.group(1)
            mes_nombre = match_fecha.group(2)
            
            # Mapeo de meses
            meses = {
                'enero': '01', 'febrero': '02', 'marzo': '03', 'abril': '04',
                'mayo': '05', 'junio': '06', 'julio': '07', 'agosto': '08',
                'septiembre': '09', 'octubre': '10', 'noviembre': '11', 'diciembre': '12'
            }
            
            if mes_nombre in meses:
                año_actual = datetime.now().year
                fecha = f"{año_actual}-{meses[mes_nombre]}-{dia.zfill(2)}"
                print(f"✅ Fecha extraída de input: {fecha}")

        
        # Buscar fecha con patrón "dd/mm/yyyy"
        elif re.search(patron_fecha_numero, input_lower):
            match = re.search(patron_fecha_numero, input_lower)
            dia, mes, año = match.groups()
            if len(año) == 2:
                año = "20" + año
            fecha = f"{año}-{mes.zfill(2)}-{dia.zfill(2)}"
            print(f"✅ Fecha extraída de input (formato numérico): {fecha}")
        
        # Buscar fecha con patrón "yyyy-mm-dd"
        elif re.search(patron_fecha_guion, input_lower):
            match = re.search(patron_fecha_guion, input_lower)
            fecha = f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
            print(f"✅ Fecha extraída de input (formato ISO): {fecha}")
        
        # Palabras clave para fechas relativas
        elif "hoy" in input_lower:
            fecha = datetime.now().strftime("%Y-%m-%d")
            print(f"✅ Fecha extraída (hoy): {fecha}")
        elif "mañana" in input_lower:
            fecha = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            print(f"✅ Fecha extraída (mañana): {fecha}")
        elif "pasado mañana" in input_lower:
            fecha = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
            print(f"✅ Fecha extraída (pasado mañana): {fecha}")
    
    # Guardar fecha en session_attributes si se extrajo
    if fecha:
        session_attributes["fecha"] = fecha
    
    # PASO 2: EXTRAER ENTIDADES ESPECÍFICAS (FILTRANDO PALABRAS GENÉRICAS)
    # 1. Buscar frases compuestas primero (ej: "san juan")
    for i in range(len(palabras) - 1):
        frase = f"{palabras[i]} {palabras[i+1]}"
        # Verificar que la frase no sea genérica
        if not any(p in frase for p in palabras_genericas):
            # Buscar sede con frase compuesta
            if not sede_id:
                test_sede_id = obtener_id_sede(frase)
                if test_sede_id:
                    sede_id = test_sede_id
                    sede_nombre = obtener_nombre_sede_por_id(test_sede_id)
                    session_attributes["sede_id"] = str(sede_id)
                    session_attributes["sede_nombre"] = sede_nombre
                    print(f"✅ Sede detectada (frase): {sede_nombre} (ID: {sede_id})")
                    
                    # AUTO-DETECTAR CIUDAD POR SEDE
                    if not ciudad_id:
                        ciudad_info = obtener_ciudad_fallback_por_sede(sede_id)
                        if ciudad_info:
                            ciudad_id = ciudad_info["id"]
                            ciudad = ciudad_info["nombre"]
                            session_attributes["ciudad_id"] = str(ciudad_id)
                            session_attributes["ciudad_nombre"] = ciudad
                            print(f"✅ Ciudad auto-detectada por sede: {ciudad} (ID: {ciudad_id})")
                    break
    
    # 2. Buscar ciudad por nombre (SOLO PALABRAS NO GENÉRICAS)
    if not ciudad_id:
        for palabra in palabras:
            if palabra not in palabras_genericas:
                # Crear estructura de slots temporal para validar ciudad
                slots_temp = {"ciudad": {"value": {"interpretedValue": palabra}}}
                try:
                    ciudad_id_temp, ciudad_nombre_temp, session_attributes_temp, _ = validar_ciudad_usuario(
                        slots_temp, session_attributes, palabra, intent
                    )
                    if ciudad_id_temp:
                        ciudad_id = ciudad_id_temp
                        ciudad = ciudad_nombre_temp
                        session_attributes["ciudad_id"] = str(ciudad_id)
                        session_attributes["ciudad_nombre"] = ciudad
                        print(f"✅ Ciudad detectada: {ciudad} (ID: {ciudad_id})")
                        break
                except Exception as e:
                    print(f"❌ Error validando ciudad '{palabra}': {e}")
                    continue
    
    # 3. Buscar clase en el input (SOLO PALABRAS NO GENÉRICAS Y CON CUTOFF ALTO)
    if not clase_id:
        # Primero buscar coincidencias exactas
        for palabra in palabras:
            if palabra not in palabras_genericas and len(palabra) >= 4:  # ✅ Mínimo 4 caracteres
                test_clase_id = obtener_id_actividad_estricto(palabra)  # ✅ Función más estricta
                if test_clase_id:
                    # Verificar si es coincidencia exacta
                    nombre_actividad = obtener_nombre_actividad_por_id(test_clase_id)
                    if nombre_actividad and palabra.lower() == nombre_actividad.lower():
                        clase_id = test_clase_id
                        clase_nombre = nombre_actividad
                        session_attributes["clase_id"] = str(clase_id)
                        session_attributes["clase_nombre"] = clase_nombre
                        print(f"✅ Clase detectada (exacta): {clase_nombre} (ID: {clase_id})")
                        break
        
        # Si no hay coincidencia exacta, buscar aproximadas con cutoff alto
        if not clase_id:
            for palabra in palabras:
                if palabra not in palabras_genericas and len(palabra) >= 5:  # ✅ Mínimo 5 caracteres para aproximadas
                    test_clase_id = obtener_id_actividad_estricto(palabra)
                    if test_clase_id:
                        clase_id = test_clase_id
                        clase_nombre = obtener_nombre_actividad_por_id(test_clase_id)
                        session_attributes["clase_id"] = str(clase_id)
                        session_attributes["clase_nombre"] = clase_nombre
                        print(f"✅ Clase detectada (aproximada): {clase_nombre} (ID: {clase_id})")
                        break
    
    # 4. Buscar sede individual solo si no se encontró con frases compuestas (FILTRADO)
    if not sede_id:
        for palabra in palabras:
            if palabra not in palabras_genericas and len(palabra) >= 4:  # ✅ Mínimo 4 caracteres
                test_sede_id = obtener_id_sede_estricto(palabra)  # ✅ Función más estricta
                if test_sede_id:
                    sede_id = test_sede_id
                    sede_nombre = obtener_nombre_sede_por_id(test_sede_id)
                    session_attributes["sede_id"] = str(sede_id)
                    session_attributes["sede_nombre"] = sede_nombre
                    print(f"✅ Sede detectada: {sede_nombre} (ID: {sede_id})")
                    
                    # AUTO-DETECTAR CIUDAD POR SEDE
                    if not ciudad_id:
                        ciudad_info = obtener_ciudad_fallback_por_sede(sede_id)
                        if ciudad_info:
                            ciudad_id = ciudad_info["id"]
                            ciudad = ciudad_info["nombre"]
                            session_attributes["ciudad_id"] = str(ciudad_id)
                            session_attributes["ciudad_nombre"] = ciudad
                            print(f"✅ Ciudad auto-detectada por sede: {ciudad} (ID: {ciudad_id})")
                    break
    
    print(f"🔍 Estado después de extracción - ciudad_id: {ciudad_id}, sede_id: {sede_id}, clase_id: {clase_id}, fecha: {fecha}")
    
    # ===== LÓGICA DE CASOS =====
    
    # CASO A: Tenemos ciudad, sede, clase y fecha - COMPLETAMENTE LISTO
    if ciudad_id and sede_id and clase_id and fecha:
        print("✅ CASO A: Datos completos - listo para consulta")
        # Actualizar session_attributes con todos los datos
        session_attributes["ciudad_id"] = str(ciudad_id)
        session_attributes["ciudad_nombre"] = ciudad
        session_attributes["sede_id"] = str(sede_id)
        session_attributes["sede_nombre"] = sede_nombre
        session_attributes["clase_id"] = str(clase_id)
        session_attributes["clase_nombre"] = clase_nombre
        session_attributes["fecha"] = fecha
        session_attributes["tipo_consulta_implícita"] = "2"  # Consulta específica de clase
        print(f"🎯 CONSULTA DIRECTA: {clase_nombre} en {sede_nombre} ({ciudad}) para {fecha}")
        
        return {
            "ciudad_id": int(ciudad_id),
            "ciudad_nombre": ciudad,
            "sede_id": int(sede_id),
            "sede_nombre": sede_nombre,
            "clase_id": int(clase_id),
            "clase_nombre": clase_nombre,
            "fecha": fecha,
            "tipo_consulta": "2",  # Horarios de una clase específica
            "consulta_directa": True,  # Flag para indicar que está listo
            "session_attributes": session_attributes
        }
    
    # CASO B: Tenemos ciudad, sede y fecha (SIN clase) - Asumir consulta tipo 1 (todas las clases)
    elif ciudad_id and sede_id and fecha and not clase_id:
        print("✅ CASO B: Ciudad + Sede + Fecha (sin clase) - Consulta tipo 1")
        # Actualizar session_attributes con todos los datos
        session_attributes["ciudad_id"] = str(ciudad_id)
        session_attributes["ciudad_nombre"] = ciudad
        session_attributes["sede_id"] = str(sede_id)
        session_attributes["sede_nombre"] = sede_nombre
        session_attributes["fecha"] = fecha
        session_attributes["tipo_consulta_implícita"] = "1"  # Todas las clases para esa fecha
        print(f"🎯 CONSULTA DIRECTA: Todas las clases en {sede_nombre} ({ciudad}) para {fecha}")
        
        return {
            "ciudad_id": int(ciudad_id),
            "ciudad_nombre": ciudad,
            "sede_id": int(sede_id),
            "sede_nombre": sede_nombre,
            "clase_id": None,
            "clase_nombre": None,
            "fecha": fecha,
            "tipo_consulta": "1",
            "consulta_directa": True,  # Flag para indicar que está listo
            "session_attributes": session_attributes
        }
    
    # CASO C: Solo fecha sin otros parámetros - Preguntar ciudad
    elif fecha and not ciudad_id and not sede_id and not clase_id:
        print("✅ CASO C: Solo fecha detectada - Preguntar ciudad")
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {
                    "name": "ConsultaGrupales",
                    "slots": intent.get("slots", {}),
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": f"Perfecto, veo que quieres consultar para el {fecha}. 📅\n\n¿En qué ciudad deseas consultar las clases grupales?"
            }]
        }
    
    # CASO D: Tenemos ciudad, sede y clase (SIN fecha) - Preguntar fecha
    elif ciudad_id and sede_id and clase_id and not fecha:
        print("✅ CASO D: Ciudad + Sede + Clase (sin fecha) - Preguntar fecha")
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "fecha"},
                "intent": {
                    "name": "ConsultaGrupales",
                    "slots": intent.get("slots", {}),
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": f"Perfecto, veo que quieres consultar {clase_nombre} en la sede {sede_nombre}. 📅\n\n¿Para qué fecha? (Ejemplo: hoy, mañana, 15 de agosto, etc.)"
            }]
        }
    
    # Si tenemos algunos parámetros, retornar lo que tenemos para continuar flujo normal
    if ciudad_id or sede_id or clase_id:
        print("✅ Tenemos algunos parámetros - retornando para flujo normal de ConsultaGrupales")
        resultado = {"session_attributes": session_attributes}
        if ciudad_id:
            resultado["ciudad_id"] = ciudad_id
            resultado["ciudad_nombre"] = ciudad
        if sede_id:
            resultado["sede_id"] = sede_id
            resultado["sede_nombre"] = sede_nombre
        if clase_id:
            resultado["clase_id"] = clase_id
            resultado["clase_nombre"] = clase_nombre
        if fecha:
            resultado["fecha"] = fecha
        return resultado
    
    # Si no encontramos nada, retornar None para flujo normal
    print("❌ No se detectaron parámetros - continuando flujo normal")
    return None

def obtener_id_actividad_estricto(nombre_actividad):
    """
    Versión más estricta de obtener_id_actividad que evita falsos positivos
    """
    print(f"🔍 ===== DEBUG OBTENER_ID_ACTIVIDAD_ESTRICTO =====")
    print(f"🔍 Input original: '{nombre_actividad}'")
    
    # ✅ FILTRO: Palabras muy cortas o genéricas
    if len(nombre_actividad) < 4:
        print(f"❌ Palabra muy corta: '{nombre_actividad}' (mínimo 4 caracteres)")
        return None
    
    actividades_map = {
        "fitball": 7,
        "pilates reformer": 10,
        "pilates mat": 19,
        "bodybalance": 25,
        "bodypump": 29,
        "g.a.p": 37,
        "bodycombat": 38,
        "fitcombat": 40,
        "cyclingtech endurance": 52,
        "cyclingtech challenge": 54,
        "bungee": 57,
        "funcional cross": 59,
        "rumba": 42,
        "pilates reformer studio": 72,
        "b ride": 113,
        "b shape": 114,
        "stretching xp": 21,
        "barre": 26,
        "glúteo xp": 30,
        "glúteo": 31,
        "power boxing": 41,
        "aeróbicos": 43,
        "step": 45,
        "core": 109,
        "aerologic": 110,
        "tono": 111,
        "sexy dance": 112,
        "stretching": 22,
        "crosstech": 27,
        "rip 60": 28,
        "abdomen xp": 33,
        "hiit grupal": 35,
        "tae bo": 39,
        "danzika": 44,
        "danztep": 46,
        "zumba": 47,
        "cyclingtech": 51,
        "consulta nutricion": 14,
        "yoga": 20,
        "abdomen": 32,
        "bootcamp": 34,
        "tabata x5": 36,
        "zumba step": 48,
        "bodyattack": 49,
        "strong": 50,
        "cyclingtech hiit": 53,
        "sprint": 55,
        "danza arabe": 61,
        "zonas humedas": 65,
        "hit funcional": 69,
        "power jump": 75,
    }
    
    nombre_normalizado = normalizar_nombre(nombre_actividad)
    print(f"🔍 Nombre normalizado: '{nombre_normalizado}'")
    
    actividades_map_normalizado = {normalizar_nombre(k): v for k, v in actividades_map.items()}

    # 1. Búsqueda exacta
    if nombre_normalizado in actividades_map_normalizado:
        print(f"✅ ENCONTRADO EXACTO: '{nombre_normalizado}'")
        return actividades_map_normalizado[nombre_normalizado]

    # 2. Coincidencia parcial SOLO si el input es suficientemente específico
    if len(nombre_normalizado) >= 5:  # ✅ Mínimo 5 caracteres para coincidencia parcial
        for k, v in actividades_map_normalizado.items():
            # A. El input está contenido en la actividad (y es suficientemente largo)
            if len(nombre_normalizado) >= 5 and nombre_normalizado in k:
                print(f"✅ ENCONTRADO (input en actividad): '{nombre_normalizado}' in '{k}'")
                return v
            
            # B. La actividad está contenida en el input (solo actividades específicas)
            if len(k) >= 4 and k in nombre_normalizado:
                print(f"✅ ENCONTRADO (actividad en input): '{k}' in '{nombre_normalizado}'")
                return v

    # 3. Búsqueda con difflib SOLO con cutoff alto
    import difflib
    cutoff_alto = 0.8  # ✅ Cutoff muy alto para evitar falsos positivos
    matches = difflib.get_close_matches(nombre_normalizado, actividades_map_normalizado.keys(), n=1, cutoff=cutoff_alto)
    if matches:
        print(f"✅ Actividad corregida (difflib estricto): '{nombre_actividad}' → '{matches[0]}'")
        return actividades_map_normalizado[matches[0]]

    print("❌ NO ENCONTRADO (estricto)")
    return None

def obtener_id_sede_estricto(nombre_sede):
    """
    Versión más estricta de obtener_id_sede que evita falsos positivos
    """
    print(f"===== DEBUG OBTENER_ID_SEDE_ESTRICTO =====")
    print(f"Input original: '{nombre_sede}'")
    
    # ✅ FILTRO: Palabras muy cortas o genéricas
    if len(nombre_sede) < 4:
        print(f"❌ Palabra muy corta: '{nombre_sede}' (mínimo 4 caracteres)")
        return None
    
    # Verificar si el input es una ciudad conocida
    ciudades_conocidas = [
        "bogota", "medellin", "cali", "barranquilla", "bucaramanga", "cartagena", 
        "pereira", "armenia", "manizales", "villavicencio", "ibague", "cucuta", 
        "pasto", "tunja", "palmira", "neiva", "monteria", "valledupar", 
        "soacha", "bello", "chia", "envigado", "dosquebradas", "tulua"
    ]
    
    nombre_normalizado = normalizar_nombre(nombre_sede)
    print(f"Nombre normalizado: '{nombre_normalizado}'")
    
    # Si el input es una ciudad, retornar None inmediatamente
    if nombre_normalizado in [normalizar_nombre(c) for c in ciudades_conocidas]:
        print(f"❌ '{nombre_sede}' es una CIUDAD, no una sede. Retornando None.")
        return None
        
    sedes_map = {
        "ejecutivos": 90,
        "dos quebradas": 68,
        "viva villavicencio": 92,
        "bocagrande": 86,
        "llanocentro": 43,
        "titan plaza": 99,
        "calle 90": 122,
        "autopista 170": 11,
        "gran estacion": 107,
        "torre central": 41,
        "normandia": 7,
        "colina": 24,
        "carrera 11": 6,
        "autopista 135": 10,
        "bulevar": 102,
        "kennedy": 3,
        "diverplaza": 106,
        "pasadena": 35,
        "pablo vi": 34,
        "country 138": 116,
        "chipichape": 23,
        "ensueño": 123,
        "plaza central": 103,
        "terreros": 117,
        "oeste": 71,
        "floresta": 53,
        "plaza bosa": 115,
        "paseo del rio": 126,
        "santa ana": 127,
        "tulua": 91,
        "palmira": 96,
        "hayuelos": 27,
        "sultana": 40,
        "centro mayor": 8,
        "suba": 39,
        "chia": 22,
        "cacique": 85,
        "caney": 94,
        "llanogrande": 125,
        "chico": 2,
        "ibague": 72,
        "vizcaya": 47,
        "san lucas": 66,
        "fontanar": 111,
        "antares": 105,
        "villagrande": 69,
        "laureles": 44,
        "belen": 76,
        "americas": 75,
        "camino real": 77,
        "san juan": 80,
        "cucuta": 25,
        "plazuela": 36,
        "galerias": 110,
        "cabrera": 17,
        "pasto": 87,
        "mall del este": 81,
        "superior": 81,
        "manizales": 46,
        "santa maria de los angeles": 48,
        "vegas": 48,
        "pereira": 50,
        "armenia": 70,
        "robledo": 55,
        "avenida colombia": 58,
        "colombia": 58,
        "megamall": 15,
        "premium plaza": 45,
        "cedritos": 20,
        "city plaza": 59,
        "portal 80": 37,
        "chapinero": 21,
        "miramar": 32,
        "recreo": 56,
        "jardin plaza": 29,
        "viva barranquilla": 101,
        "parque washington": 112,
        "washington": 112,
        "niquia": 57,
        "caracoli": 63,
        "caribe plaza": 19,
        "calle 122 studio": 152,
        "gran manzana": 185,
        "tunja": 150,
        "connecta": 151
    }
    
    # 1. Búsqueda exacta con normalización
    for sede_key in sedes_map:
        if nombre_normalizado == normalizar_nombre(sede_key):
            resultado = sedes_map[sede_key]
            print(f"✅ ENCONTRADO EXACTO: '{nombre_sede}' → '{sede_key}' (ID: {resultado})")
            return resultado
    
    # 2. Búsqueda con difflib SOLO con cutoff alto
    import difflib
    todas_sedes = list(sedes_map.keys())
    cutoff_alto = 0.8  # ✅ Cutoff muy alto para evitar falsos positivos
    matches = difflib.get_close_matches(nombre_sede.lower(), todas_sedes, n=1, cutoff=cutoff_alto)
    if matches:
        sede_encontrada = matches[0]
        resultado = sedes_map[sede_encontrada]
        print(f"✅ ENCONTRADO (difflib estricto): '{nombre_sede}' → '{sede_encontrada}' (ID: {resultado})")
        return resultado
    
    print(f"❌ NO ENCONTRADO (estricto): '{nombre_sede}'")
    print("===== FIN DEBUG OBTENER_ID_SEDE_ESTRICTO =====")
    return None

def buscar_sede_similar(texto):
    """Busca una sede similar al texto proporcionado"""
    try:
        sede_id = obtener_id_sede(normalizar_nombre(texto))
        if sede_id:
            return obtener_nombre_sede_por_id(sede_id)
    except:
        pass
    return None

def get_actividades_map_normalizado():
    actividades_map = {
        "fitball": 7, "fitball": 24,
        "pilates reformer": 10,
        "pilates mat": 19,
        "bodybalance": 25,
        "bodypump": 29,
        "g.a.p": 37,
        "bodycombat": 38,
        "fitcombat": 40,
        "cyclingtech endurance": 52,
        "cyclingtech challenge": 54,
        "bungee": 57,
        "funcional cross": 59,
        "rumba": 42,
        "pilates reformer studio": 72,
        "b ride": 113,
        "b shape": 114,
        "stretching xp": 21,
        "barre": 26,
        "glúteo xp": 30,
        "glúteo": 31,
        "power boxing": 41,
        "aeróbicos": 43,
        "step": 45,
        "core": 109,
        "aerologic": 110,
        "tono": 111,
        "sexy dance": 112,
        "stretching": 22,
        "crosstech": 27,
        "rip 60": 28,
        "abdomen xp": 33,
        "hiit grupal": 35,
        "tae bo": 39,
        "danzika": 44,
        "danztep": 46,
        "zumba": 47,
        "cyclingtech": 127, "cyclingtech": 51,
        "consulta nutricion": 14,
        "yoga": 20,
        "abdomen": 32,
        "bootcamp": 34,
        "tabata x5": 36,
        "zumba step": 48,
        "bodyattack": 49,
        "strong": 50,
        "cyclingtech hiit": 53,
        "sprint": 55,
        "danza arabe": 61,
        "zonas humedas": 65,
        "hit funcional": 69,
        "power jump": 75,
        # Puedes agregar más variantes o sinónimos aquí si lo necesitas
    }
    def normalizar_nombre(nombre):
        nombre = nombre.lower().strip()
        nombre_sin_tildes = ''.join(c for c in unicodedata.normalize('NFD', nombre) if unicodedata.category(c) != 'Mn')
        nombre_sin_tildes = nombre_sin_tildes.replace('ñ', 'n')
        nombre_limpio = re.sub(r'[^\w\s]', '', nombre_sin_tildes)
        nombre_limpio = re.sub(r'\s+', ' ', nombre_limpio).strip()
        return nombre_limpio

    return {normalizar_nombre(k): v for k, v in actividades_map.items()}




def buscar_sede_similar(nombre_usuario):
    """
    Busca la sede más similar al input del usuario en la lista global de sedes válidas.
    """
    sedes_validas = obtener_sedes_validas()
    nombre_normalizado = normalizar_nombre(nombre_usuario)
    sedes_normalizadas = [normalizar_nombre(s) for s in sedes_validas]
    import difflib
    matches = difflib.get_close_matches(nombre_normalizado, sedes_normalizadas, n=1, cutoff=0.7)
    if matches:
        idx = sedes_normalizadas.index(matches[0])
        return sedes_validas[idx]
    return None

def corregir_ciudad_similar(ciudad_usuario):
    ciudades_validas = obtener_ciudades_validas()
    import difflib
    ciudad_normalizada = normalizar_nombre(ciudad_usuario)
    ciudades_normalizadas = [normalizar_nombre(c) for c in ciudades_validas]
    matches = difflib.get_close_matches(ciudad_normalizada, ciudades_normalizadas, n=1, cutoff=0.7)
    if matches:
        idx = ciudades_normalizadas.index(matches[0])
        return ciudades_validas[idx]
    return ciudad_usuario

def corregir_ciudad_en_input(input_transcript):
    """Corrige ciudades en el input SIN alterar fechas"""
    # Verificar si el input contiene una fecha válida
    fecha_normalizada, _ = normalizar_fecha(input_transcript.strip())
    if fecha_normalizada:
        print(f"🔍 Input contiene fecha válida, NO corregir ciudades: '{input_transcript}'")
        return input_transcript
    
    ciudades_validas = obtener_ciudades_validas()
    import difflib
    palabras = input_transcript.split()
    palabras_corregidas = []
    
    for palabra in palabras:
        # Saltar corrección si la palabra es parte de una fecha
        if palabra.lower() in ["de", "del", "para", "el", "en", "agosto", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "septiembre", "octubre", "noviembre", "diciembre"]:
            palabras_corregidas.append(palabra)
            continue
            
        palabra_norm = normalizar_nombre(palabra)
        ciudades_normalizadas = [normalizar_nombre(c) for c in ciudades_validas]
        matches = difflib.get_close_matches(palabra_norm, ciudades_normalizadas, n=1, cutoff=0.8)  # Cutoff más alto
        if matches:
            idx = ciudades_normalizadas.index(matches[0])
            palabras_corregidas.append(ciudades_validas[idx])
        else:
            palabras_corregidas.append(palabra)
    return " ".join(palabras_corregidas)

def corregir_sede_en_input(input_transcript):
    """Corrige sedes en el input SIN alterar fechas"""
    # Verificar si el input contiene una fecha válida
    fecha_normalizada, _ = normalizar_fecha(input_transcript.strip())
    if fecha_normalizada:
        print(f"🔍 Input contiene fecha válida, NO corregir sedes: '{input_transcript}'")
        return input_transcript
    
    sedes_validas = obtener_sedes_validas()
    import difflib
    palabras = input_transcript.split()
    palabras_corregidas = []
    sedes_normalizadas = [normalizar_nombre(s) for s in sedes_validas]
    
    for palabra in palabras:
        # Saltar corrección si la palabra es parte de una fecha o preposición
        if palabra.lower() in ["de", "del", "para", "el", "en", "que", "hay", "clases", "agosto", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "septiembre", "octubre", "noviembre", "diciembre", "hoy", "mañana"]:
            palabras_corregidas.append(palabra)

def corregir_errores_tipeo_automatico(texto, lista_valida, umbral_distancia=2):
    """
    Corrige errores de tipeo automáticamente usando distancia de Levenshtein
    Sin necesidad de hardcodear variaciones manualmente
    """
    def distancia_levenshtein(s1, s2):
        """Calcula la distancia de Levenshtein entre dos strings"""
        if len(s1) < len(s2):
            return distancia_levenshtein(s2, s1)
        
        if len(s2) == 0:
            return len(s1)
        
        previous_row = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        
        return previous_row[-1]
    
    def es_error_tipeo_probable(original, candidato, umbral):
        """Determina si es probable que sea un error de tipeo"""
        if not original or not candidato:
            return False
        
        # Calcular distancia
        distancia = distancia_levenshtein(original.lower(), candidato.lower())
        
        # Ajustar umbral según longitud de palabra
        umbral_ajustado = max(1, min(umbral, len(candidato) // 3))
        
        # Es probable error de tipeo si:
        # 1. Distancia es pequeña
        # 2. Longitudes son similares
        # 3. Comparten suficientes caracteres
        longitud_similar = abs(len(original) - len(candidato)) <= 2
        distancia_aceptable = distancia <= umbral_ajustado
        
        return distancia_aceptable and longitud_similar
    
    texto_normalizado = normalizar_nombre(texto)
    
    # 1. Buscar coincidencia exacta primero
    for item in lista_valida:
        if normalizar_nombre(item) == texto_normalizado:
            return item
    
    # 2. Buscar corrección automática por distancia de edición
    candidatos_tipeo = []
    for item in lista_valida:
        item_normalizado = normalizar_nombre(item)
        if es_error_tipeo_probable(texto_normalizado, item_normalizado, umbral_distancia):
            distancia = distancia_levenshtein(texto_normalizado, item_normalizado)
            candidatos_tipeo.append((item, distancia))
    
    # 3. Retornar el candidato con menor distancia
    if candidatos_tipeo:
        candidatos_tipeo.sort(key=lambda x: x[1])  # Ordenar por distancia
        mejor_candidato = candidatos_tipeo[0][0]
        print(f"🔧 Corrección automática: '{texto}' → '{mejor_candidato}' (distancia: {candidatos_tipeo[0][1]})")
        return mejor_candidato
    
    # 4. Fallback con difflib para casos complejos
    matches = difflib.get_close_matches(texto_normalizado, 
                                       [normalizar_nombre(item) for item in lista_valida], 
                                       n=1, cutoff=0.7)
    if matches:
        # Encontrar el item original correspondiente
        for item in lista_valida:
            if normalizar_nombre(item) == matches[0]:
                print(f"🔧 Corrección difflib: '{texto}' → '{item}'")
                return item
    
    return texto  # Si no se puede corregir, retornar original

def corregir_sedes_inteligente(texto_input):
    """
    Corrige errores de tipeo en sedes usando algoritmos automáticos
    Sin hardcodear variaciones manualmente
    """
    # Lista de sedes válidas del sistema
    sedes_validas = [
        # Bogotá
        "normandia", "chico", "centro mayor", "zona rosa", "hayuelos", "cedritos",
        "chapinero", "kennedy", "suba", "colina", "cabrera", "autopista 135",
        "fontanar", "plaza bosa", "torre central", "carrera 11", "gran estacion",
        "paseo del rio", "autopista 170", "portal 80", "diverplaza", "galerias",
        "sultana", "plaza central", "pablo vi", "bulevar", "terreros", "floresta",
        "connecta", "calle 122 studio", "ensueño", "country", "calle 90",
        "santa ana", "pasadena", "titan plaza",
        
        # Medellín
        "poblado", "laureles", "belen", "vegas", "san lucas", "vizcaya", 
        "city plaza", "avenida colombia", "mall del este", "villagrande",
        "premium plaza", "camino real", "san juan", "robledo", "llanogrande",
        "americas",
        
        # Otras ciudades
        "niquia", "caracoli", "megamall", "cacique", "chipichape", "jardin plaza",
        "oeste", "caney", "recreo", "parque washington", "viva barranquilla",
        "miramar", "caribe plaza", "plazuela", "ejecutivos", "bocagrande",
        "gran manzana", "dos quebradas", "pereira", "armenia", "manizales",
        "ibague", "cucuta", "pasto", "tulua", "palmira", "tunja", 
        "viva villavicencio", "llanocentro"
    ]
    
    # Corregir cada palabra del input
    palabras = texto_input.lower().strip().split()
    palabras_corregidas = []
    
    for palabra in palabras:
        # Saltar palabras genéricas
        if palabra in ["en", "de", "del", "la", "el", "que", "hay", "horarios", "sede"]:
            palabras_corregidas.append(palabra)
            continue
        
        # Corregir automáticamente
        palabra_corregida = corregir_errores_tipeo_automatico(palabra, sedes_validas, umbral_distancia=2)
        palabras_corregidas.append(palabra_corregida)
    
    resultado = " ".join(palabras_corregidas)
    
    if resultado != texto_input.lower().strip():
        print(f"🤖 Corrección inteligente: '{texto_input}' → '{resultado}'")
    
    return resultado

###########################################
# Extraer y validar input de ConsultarSedes
###########################################

def extraer_y_validar_slots_sedes(input_transcript, session_attributes, intent):
    """
    Extrae y valida parámetros para ConsultarSedes desde texto libre.
    Maneja: ciudad, sede, categoría, horarios
    NUEVA FUNCIONALIDAD: Prioriza detección de sede para consultas de horarios
    """
    print(f"🔍 === INICIO extraer_y_validar_slots_sedes ===")
    print(f"🔍 Input: '{input_transcript}'")
    print(f"🔍 Session attributes: {session_attributes}")
    
    slots = intent.get("slots", {})
    
    # Variables de extracción
    ciudad_id = session_attributes.get("ciudad_id")
    ciudad = session_attributes.get("ciudad_nombre") or session_attributes.get("ciudad")
    sede_id = session_attributes.get("sede_id")
    sede = session_attributes.get("sede_nombre")
    categoria_nombre = None
    categoria_detectada = None
    
    # 🆕 PASO PREVIO: DETECTAR CONSULTAS DE HORARIOS CON SEDE
    horarios_keywords = ["horarios", "horario", "atencion", "atención", "abren", "cierran", "abre", "cierra", "funciona", "tienen","apertura","cierre","cuando abren","cuando cierran","a que hora abren","a que hora cierran"]
    es_consulta_horarios = any(keyword in input_transcript.lower() for keyword in horarios_keywords)
    
    print(f"🔍 Keywords horarios: {horarios_keywords}")
    print(f"🔍 Es consulta horarios: {es_consulta_horarios}")
    print(f"🔍 Input lower: '{input_transcript.lower()}'")
    
    if es_consulta_horarios:
        print("🎯 CONSULTA DE HORARIOS DETECTADA - Priorizando detección de sede")
        
        # PRIORIDAD 1: Buscar sede PRIMERO en consultas de horarios
        palabras_sede = [
            # Sedes de Bogotá
            "country", "country 138", "country club", "club country",
            "chico", "centro mayor", "centro", "zona rosa", "hayuelos", "restrepo",
            "park way", "parkway", "plaza", "portal", "salitre", "unicentro",
            "titan", "santafe", "cedritos", "bima", "outlet", "americas",
            "normandia", "santa ana", "pasadena", "kennedy", "suba", "colina",
            "chapinero", "usaquen", "fontibon", "bosa", "engativa",
            "calle 90", "cabrera", "autopista 135", "fontanar", "plaza bosa",
            "torre central", "carrera 11", "gran estacion", "paseo del rio",
            "autopista 170", "portal 80", "diverplaza", "galerias",
            "sultana", "plaza central", "pablo vi", "bulevar", "terreros",
            "floresta", "connecta", "calle 122 studio", "ensueño", "ensueno",

            # Sedes de Medellín
            "poblado", "laureles", "envigado", "itagui", "sabaneta", "estrella",
            "belen", "vegas", "san lucas", "vizcaya", "city plaza",
            "avenida colombia", "colombia", "mall del este", "villagrande", 
            "premium plaza", "camino real", "san juan", "robledo", "llanogrande",
            
            # Sedes de otras ciudades
            "niquia", "caracoli", "megamall", "cacique", "chipichape",
            "jardin plaza", "oeste", "caney", "recreo", "parque washington",
            "washington", "viva barranquilla", "miramar", "caribe plaza",
            "plazuela", "ejecutivos", "bocagrande", "gran manzana",
            "dos quebradas", "pereira", "armenia", "manizales", "ibague",
            "cucuta", "pasto", "tulua", "palmira", "tunja", "viva villavicencio",
            "llanocentro"
        ]
        
        # 🆕 APLICAR CORRECCIÓN INTELIGENTE DE TIPEO ANTES DE NORMALIZAR
        input_corregido = corregir_sedes_inteligente(input_transcript)
        input_normalizado = normalizar_nombre(input_corregido)
        print(f"🔍 Input normalizado para sedes: '{input_transcript}' → corregido: '{input_corregido}' → normalizado: '{input_normalizado}'")
        
        for palabra_sede in palabras_sede:
            # 🆕 COMPARAR AMBOS NORMALIZADOS
            sede_normalizada = normalizar_nombre(palabra_sede)
            if sede_normalizada in input_normalizado:
                print(f"🎯 Sede encontrada en consulta de horarios: {palabra_sede} (normalizada)")
                sede_encontrada_id = obtener_id_sede(palabra_sede)
                if sede_encontrada_id:
                    session_attributes["sede_id"] = str(sede_encontrada_id)
                    session_attributes["sede_nombre"] = obtener_nombre_sede_por_id(sede_encontrada_id)
                    sede_id = sede_encontrada_id
                    sede = session_attributes["sede_nombre"]
                    print(f"✅ Sede extraída para horarios: {sede} (ID: {sede_id})")
                    
                    # Auto-asignar ciudad usando fallback
                    if not ciudad_id:
                        fallback_ciudad = obtener_ciudad_fallback_por_sede(sede_encontrada_id)
                        if fallback_ciudad:
                            ciudad_id = fallback_ciudad['id']
                            ciudad = fallback_ciudad['nombre']
                            session_attributes["ciudad_id"] = str(ciudad_id)
                            session_attributes["ciudad_nombre"] = ciudad
                            print(f"✅ Ciudad auto-asignada (fallback): {ciudad} para sede: {sede}")
                    
                    # 🆕 RETORNO DIRECTO PARA CONSULTA DE HORARIOS DE SEDE
                    if sede_id and ciudad_id:
                        print("🎯 CONSULTA DIRECTA DE HORARIOS DE SEDE DETECTADA")
                        return {
                            "consulta_directa": True,
                            "ciudad_id": int(ciudad_id),
                            "ciudad_nombre": ciudad,
                            "sede_id": int(sede_id),
                            "sede_nombre": sede,
                            "tipo_consulta": "horarios_sede",
                            "session_attributes": session_attributes
                        }
                    break
    
    # PASO 1: EXTRAER CIUDAD si no la tenemos
    if not ciudad_id and input_transcript:
        print("🔍 Intentando extraer ciudad...")
        
        # 🆕 CORRECCIÓN DE ERRORES COMUNES DE CIUDADES
        def corregir_ciudades_comunes(texto):
            """Corrige errores comunes en nombres de ciudades"""
            correcciones_ciudades = {
                # Solo errores comunes de tipeo, NO variaciones aceptables
                "bogote": "bogota", "bogto": "bogota", "bogot": "bogota",
                "medelin": "medellin", "medelein": "medellin", 
                "bucamaranga": "bucaramanga", "bucaramang": "bucaramanga",
                "cucta": "cucuta", "cuccuta": "cucuta",
                "baranquilla": "barranquilla", "varranquilla": "barranquilla",
                "perira": "pereira", "pereria": "pereira",
                "cartage": "cartagena", "cartagina": "cartagena",
                "manizles": "manizales", "manisales": "manizales",
                "villavi": "villavicencio", "villavicenci": "villavicencio",
                "armeni": "armenia", "armnia": "armenia",
                "neva": "neiva", "naiva": "neiva",
                "pato": "pasto", "pazto": "pasto"
            }
            
            texto_lower = texto.lower().strip()
            return correcciones_ciudades.get(texto_lower, texto_lower)
        
        # Lista de ciudades OFICIALES (sin variaciones)
        ciudades_oficiales = [
            "bogota", "medellin", "cali", "barranquilla", 
            "bucaramanga", "cartagena", "pereira", "armenia", "manizales", 
            "villavicencio", "ibague", "cucuta", "pasto",
            "tunja", "palmira", "neiva", "monteria", "valledupar",
            "soacha", "bello", "chia", "envigado", "dosquebradas", "tulua"
        ]
        
        # 🆕 APLICAR CORRECCIÓN ANTES DE NORMALIZAR
        input_corregido = corregir_ciudades_comunes(input_transcript)
        input_normalizado = normalizar_nombre(input_corregido)
        print(f"🔍 Input original: '{input_transcript}' → corregido: '{input_corregido}' → normalizado: '{input_normalizado}'")
        
        # Buscar coincidencia usando normalización CON LISTA OFICIAL
        for ciudad_oficial in ciudades_oficiales:
            ciudad_normalizada = normalizar_nombre(ciudad_oficial)
            if ciudad_normalizada == input_normalizado:
                print(f"🎯 Ciudad encontrada (corregida y normalizada): {ciudad_oficial}")
                try:
                    # Validar la ciudad encontrada usando la función existente
                    ciudad_id_tmp, ciudad_nombre_tmp, session_attributes_tmp, respuesta_ciudad = validar_ciudad_usuario(
                        {"ciudad": {"value": {"interpretedValue": ciudad_oficial}}},
                        session_attributes,
                        ciudad_oficial,
                        intent
                    )
                    if ciudad_id_tmp:
                        ciudad_id = ciudad_id_tmp
                        ciudad = ciudad_nombre_tmp
                        session_attributes["ciudad_id"] = str(ciudad_id)
                        session_attributes["ciudad_nombre"] = ciudad
                        print(f"✅ Ciudad extraída (corregida): {ciudad} (ID: {ciudad_id})")
                        break
                except Exception as e:
                    print(f"❌ Error validando ciudad {ciudad_oficial}: {e}")
    
    # PASO 1.5: EXTRAER SEDE con prioridad en sedes compuestas
    if not sede_id and input_transcript:
        print("🔍 Intentando extraer sede...")
        
        # 🆕 NORMALIZAR INPUT PARA COMPARACIÓN
        input_normalizado = normalizar_nombre(input_transcript.lower())
        print(f"🔍 Input normalizado: '{input_normalizado}'")
        
        # 🆕 BUSCAR SEDES COMPUESTAS PRIMERO (MÁXIMA PRIORIDAD)
        print("🔍 Buscando sedes compuestas...")
        sedes_compuestas = obtener_sedes_compuestas()
        print(f"🔍 Total sedes compuestas: {len(sedes_compuestas)}")
        
        # Verificar que "centro mayor" esté en el diccionario
        if "centro mayor" in sedes_compuestas:
            print("✅ 'centro mayor' está en sedes_compuestas")
        else:
            print("❌ 'centro mayor' NO está en sedes_compuestas")
        
        for sede_compuesta, datos_sede in sedes_compuestas.items():
            sede_normalizada = normalizar_nombre(sede_compuesta)
            print(f"🔍 Comparando '{sede_normalizada}' en '{input_normalizado}'")
            
            # Buscar la sede compuesta en el input (coincidencia exacta)
            if sede_normalizada in input_normalizado:
                sede_id = datos_sede["id"]
                sede_nombre = datos_sede["nombre"]
                session_attributes["sede_id"] = str(sede_id)
                session_attributes["sede_nombre"] = sede_nombre
                print(f"✅ Sede compuesta detectada: {sede_nombre} (ID: {sede_id})")
                
                # AUTO-DETECTAR CIUDAD POR SEDE
                if not ciudad_id:
                    ciudad_info = obtener_ciudad_fallback_por_sede(sede_id)
                    if ciudad_info:
                        ciudad_id = ciudad_info["id"]
                        ciudad = ciudad_info["nombre"]
                        session_attributes["ciudad_id"] = str(ciudad_id)
                        session_attributes["ciudad_nombre"] = ciudad
                        print(f"✅ Ciudad auto-detectada por sede: {ciudad} (ID: {ciudad_id})")
                break
        
        # Log si no se encontró sede compuesta
        if not sede_id:
            print("❌ No se encontró ninguna sede compuesta en el input")
        else:
            print(f"✅ SEDE COMPUESTA ENCONTRADA - TERMINANDO BÚSQUEDA: {sede_nombre} (ID: {sede_id})")
            # Si encontramos sede compuesta, NO continuar con búsquedas adicionales
            # Actualizar session_attributes
            session_attributes["sede_id"] = str(sede_id)
            session_attributes["sede_nombre"] = sede_nombre
            if ciudad_id:
                session_attributes["ciudad_id"] = str(ciudad_id)
                session_attributes["ciudad_nombre"] = ciudad
            
            return {
                "session_attributes": session_attributes,
                "ciudad_id": ciudad_id,
                "ciudad_nombre": ciudad,
                "sede_id": sede_id,
                "sede_nombre": sede_nombre,
                "clase_id": session_attributes.get("clase_id"),
                "clase_nombre": session_attributes.get("clase_nombre"),
                "fecha": session_attributes.get("fecha")
            }
    
    # PASO 2: EXTRAER SEDE si tenemos ciudad pero no sede (Y no se encontró en sedes compuestas)
    if not sede_id and input_transcript:
        print("🔍 No se encontró sede compuesta, intentando búsqueda normal...")
        
        # Lista de sedes comunes COMPLETA
        palabras_sede = [
            # Sedes de Bogotá
            "chico", "centro mayor", "centro", "zona rosa", "hayuelos", "restrepo",
            "park way", "parkway", "plaza", "portal", "salitre", "unicentro",
            "titan", "santafe", "cedritos", "bima", "outlet", "americas",
            "normandia", "santa ana", "pasadena", "kennedy", "suba", "colina",
            "chapinero", "usaquen", "fontibon", "bosa", "engativa",
            "calle 90", "cabrera", "autopista 135", "fontanar", "plaza bosa",
            "torre central", "carrera 11", "gran estacion", "paseo del rio",
            "autopista 170", "portal 80", "diverplaza", "galerias",
            "sultana", "plaza central", "pablo vi", "bulevar", "terreros",
            "floresta", "connecta", "calle 122 studio", "country",
            # 🆕 AGREGAR ENSUEÑO Y OTRAS SEDES FALTANTES
            "ensueño", "ensueno",  # Ambas versiones
            
            # Sedes de Medellín
            "poblado", "laureles", "envigado", "itagui", "sabaneta", "estrella",
            "belen", "vegas", "san lucas", "vizcaya", "city plaza",
            "avenida colombia", "colombia", "mall del este", "villagrande", 
            "premium plaza", "camino real", "san juan", "robledo", "llanogrande",
            
            # Sedes de otras ciudades
            "niquia", "caracoli", "megamall", "cacique", "chipichape",
            "jardin plaza", "oeste", "caney", "recreo", "parque washington",
            "washington", "viva barranquilla", "miramar", "caribe plaza",
            "plazuela", "ejecutivos", "bocagrande", "gran manzana",
            "dos quebradas", "pereira", "armenia", "manizales", "ibague",
            "cucuta", "pasto", "tulua", "palmira", "tunja", "viva villavicencio",
            "llanocentro"
        ]
        
        # 🆕 APLICAR CORRECCIÓN INTELIGENTE DE TIPEO ANTES DE NORMALIZAR (PASO 2)
        input_corregido = corregir_sedes_inteligente(input_transcript)
        input_normalizado = normalizar_nombre(input_corregido)
        print(f"🔍 Input normalizado para sedes (PASO 2): '{input_transcript}' → corregido: '{input_corregido}' → normalizado: '{input_normalizado}'")
        
        for palabra_sede in palabras_sede:
            # 🆕 COMPARAR AMBOS NORMALIZADOS
            sede_normalizada = normalizar_nombre(palabra_sede)
            if sede_normalizada in input_normalizado:
                print(f"🎯 Posible sede encontrada en input: {palabra_sede} (normalizada)")
                sede_encontrada_id = obtener_id_sede(palabra_sede)
                if sede_encontrada_id:
                    session_attributes["sede_id"] = str(sede_encontrada_id)
                    session_attributes["sede_nombre"] = obtener_nombre_sede_por_id(sede_encontrada_id)
                    sede_id = sede_encontrada_id
                    sede = session_attributes["sede_nombre"]
                    print(f"✅ Sede extraída: {sede} (ID: {sede_id})")
                    break
    
    # PASO 3: EXTRAER CATEGORÍA
    if input_transcript:
        print("🔍 Intentando extraer categoría...")
        
        # Obtener categorías disponibles
        categorias_disponibles = obtener_categorias_por_linea("bodytech")
        categorias_normalizadas = [normalizar_nombre(c) for c in categorias_disponibles]
        
        input_lower = input_transcript.lower()
        input_normalizado = normalizar_nombre(input_lower)
        
        # Buscar categorías específicas en el input
        for i, categoria_norm in enumerate(categorias_normalizadas):
            if categoria_norm in input_normalizado:
                categoria_detectada = categorias_disponibles[i]
                categoria_nombre = categoria_detectada
                print(f"✅ Categoría extraída: {categoria_detectada}")
                break
        
        # También buscar palabras clave generales de categoría
        palabras_categoria = ["categoria", "categoría", "tipo", "tipos", "categorias", "categorías"]
        if any(palabra in input_lower for palabra in palabras_categoria) and not categoria_detectada:
            print("🎯 Palabra genérica 'categoría' detectada")
            categoria_detectada = "generico"  # Flag para indicar que quiere ver categorías
    
    print(f"🔍 Parámetros extraídos:")
    print(f"🔍   - Ciudad: {ciudad} (ID: {ciudad_id})")
    print(f"🔍   - Sede: {sede} (ID: {sede_id})")
    print(f"🔍   - Categoría: {categoria_detectada}")
    
    # CASOS DE RESPUESTA
    
    # CASO A: Solo categoría específica sin ciudad - Preguntar ciudad
    if categoria_detectada and categoria_detectada != "generico" and not ciudad_id:
        print("✅ CASO A: Categoría específica sin ciudad - Preguntar ciudad")
        session_attributes["categoria_detectada"] = categoria_detectada
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {
                    "name": "ConsultarSedes",
                    "slots": intent.get("slots", {}),
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": f"¡Perfecto! Veo que quieres consultar sedes de {categoria_detectada}. 🏢\n\n¿En qué ciudad deseas consultar las sedes de {categoria_detectada}?"
            }]
        }
    
    # CASO B: Categoría específica + ciudad - Mostrar sedes de esa categoría
    elif categoria_detectada and categoria_detectada != "generico" and ciudad_id:
        print("✅ CASO B: Categoría específica + ciudad - Consulta directa")
        
        # Actualizar session_attributes
        session_attributes["categoria_detectada"] = categoria_detectada
        session_attributes["pregunta_categoria"] = "si"
        
        # Poblar slot de categoría
        intent["slots"]["categoria"] = {
            "value": {
                "originalValue": categoria_detectada,
                "resolvedValues": [categoria_detectada],
                "interpretedValue": categoria_detectada
            },
            "shape": "Scalar"
        }
        
        return {
            "consulta_directa": True,
            "ciudad_id": int(ciudad_id),
            "ciudad_nombre": ciudad,
            "categoria_nombre": categoria_detectada,
            "tipo_consulta": "categoria_especifica",
            "session_attributes": session_attributes,
            "intent_actualizado": intent
        }
    
    # CASO C: Palabra genérica "categoría" sin ciudad - Preguntar ciudad
    elif categoria_detectada == "generico" and not ciudad_id:
        print("✅ CASO C: Palabra genérica 'categoría' sin ciudad - Preguntar ciudad")
        session_attributes["mostrar_categorias"] = "true"
        
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {
                    "name": "ConsultarSedes",
                    "slots": intent.get("slots", {}),
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": "¡Perfecto! Te ayudo a consultar las categorías de sedes. 🏢\n\n¿En qué ciudad deseas consultar las categorías disponibles?"
            }]
        }
    
    # CASO D: Palabra genérica "categoría" + ciudad - Mostrar categorías
    elif categoria_detectada == "generico" and ciudad_id:
        print("✅ CASO D: Palabra genérica 'categoría' + ciudad - Mostrar categorías")
        session_attributes["mostrar_categorias"] = "true"
        session_attributes["pregunta_categoria"] = "pendiente"
        
        return {
            "consulta_directa": True,
            "ciudad_id": int(ciudad_id),
            "ciudad_nombre": ciudad,
            "tipo_consulta": "mostrar_categorias",
            "session_attributes": session_attributes
        }
    
    # 🆕 CASO ESPECIAL: Consultando horarios y sede detectada
    if (session_attributes.get("consultando_horarios") == "preguntando" and 
        sede_id and ciudad_id):
        print("🎯 CONSULTA DIRECTA DE HORARIOS EN MODO PREGUNTANDO")
        
        # Actualizar session_attributes
        session_attributes["sede_id"] = str(sede_id)
        session_attributes["sede_nombre"] = sede
        session_attributes["consultando_horarios"] = "ejecutando"
        
        return {
            "consulta_directa": True,
            "ciudad_id": int(ciudad_id),
            "ciudad_nombre": ciudad,
            "sede_id": int(sede_id),
            "sede_nombre": sede,
            "tipo_consulta": "horarios_sede",
            "session_attributes": session_attributes
        }
    
    # CASO E: Solo ciudad - Flujo normal
    elif ciudad_id and not sede_id and not categoria_detectada:
        print("✅ CASO E: Solo ciudad - Continuar flujo normal")
        return {
            "ciudad_id": int(ciudad_id),
            "ciudad_nombre": ciudad,
            "session_attributes": session_attributes
        }
    
    # CASO F: Ciudad + sede - Mostrar info de sede específica
    elif ciudad_id and sede_id:
        print("✅ CASO F: Ciudad + sede - Mostrar info específica")
        return {
            "consulta_directa": True,
            "ciudad_id": int(ciudad_id),
            "ciudad_nombre": ciudad,
            "sede_id": int(sede_id),
            "sede_nombre": sede,
            "tipo_consulta": "sede_especifica",
            "session_attributes": session_attributes
        }
    
    # CASO G: Sin parámetros detectados - Preguntar ciudad
    else:
        print("✅ CASO G: Sin parámetros - Preguntar ciudad")
        return {
            "sessionState": {
                "dialogAction": {"type": "ElicitSlot", "slotToElicit": "ciudad"},
                "intent": {
                    "name": "ConsultarSedes",
                    "slots": intent.get("slots", {}),
                    "state": "InProgress"
                },
                "sessionAttributes": session_attributes
            },
            "messages": [{
                "contentType": "PlainText",
                "content": "¿En qué ciudad deseas consultar las sedes?"
            }]
        }
