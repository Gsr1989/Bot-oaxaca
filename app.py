from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
import asyncio
import qrcode
from io import BytesIO
import random

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_OAXACA = "oaxaca_plantilla_imagen.pdf"
PLANTILLA_OAXACA_SEGUNDA = "oaxacaverga.pdf"

# URL de consulta base
URL_CONSULTA_BASE = "https://oaxaca-gob-semovi.onrender.com"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT PARA OAXACA ------------
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}
user_folios = {}     # {user_id: [lista_de_folios_activos]}

async def eliminar_folio_automatico_oaxaca(folio: str):
    """Elimina folio automáticamente después del tiempo límite"""
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        
        # Notificar al usuario
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO OAXACA\n\n"
                f"El folio {folio} ha sido eliminado del sistema por falta de pago.\n\n"
                f"🌮 Para tramitar un nuevo permiso use /permiso"
            )
        
        limpiar_timer_folio(folio)
            
    except Exception as e:
        print(f"Error eliminando folio Oaxaca {folio}: {e}")

async def enviar_recordatorio_oaxaca(folio: str, minutos_restantes: int):
    """Envía recordatorios de pago para Oaxaca"""
    try:
        if folio not in timers_activos:
            return
            
        user_id = timers_activos[folio]["user_id"]
        
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO OAXACA\n\n"
            f"🌮 Folio: {folio}\n"
            f"⏰ Tiempo restante: {minutos_restantes} minutos\n"
            f"💰 Monto: $500 pesos\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite."
        )
    except Exception as e:
        print(f"Error enviando recordatorio Oaxaca para folio {folio}: {e}")

async def iniciar_timer_pago_oaxaca(user_id: int, folio: str):
    """Inicia el timer de 2 horas con recordatorios para Oaxaca"""
    async def timer_task():
        start_time = datetime.now()
        print(f"[TIMER OAXACA] Iniciado para folio {folio}, usuario {user_id}")
        
        # Recordatorios cada 30 minutos
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)  # 30 minutos
            
            if folio not in timers_activos:
                print(f"[TIMER OAXACA] Cancelado para folio {folio}")
                return
                
            minutos_restantes = 120 - minutos
            await enviar_recordatorio_oaxaca(folio, minutos_restantes)
        
        # Último recordatorio a los 110 minutos
        await asyncio.sleep(20 * 60)
        if folio in timers_activos:
            await enviar_recordatorio_oaxaca(folio, 10)
        
        # Esperar 10 minutos finales
        await asyncio.sleep(10 * 60)
        
        if folio in timers_activos:
            print(f"[TIMER OAXACA] Expirado para folio {folio}")
            await eliminar_folio_automatico_oaxaca(folio)
    
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA OAXACA] Timer iniciado para folio {folio}, total timers: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico cuando el usuario paga"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA OAXACA] Timer cancelado para folio {folio}")

def limpiar_timer_folio(folio: str):
    """Limpia todas las referencias de un folio tras expirar"""
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    """Obtiene todos los folios activos de un usuario"""
    return user_folios.get(user_id, [])

# ------------ FOLIO OAXACA CON PERSISTENCIA ------------
FOLIO_PREFIJO = "1"
folio_counter = {"siguiente": 670}

def obtener_siguiente_folio():
    folio_num = folio_counter["siguiente"]
    folio = f"{FOLIO_PREFIJO}{folio_num}"
    folio_counter["siguiente"] += 1
    return folio

def inicializar_folio_desde_supabase():
    """Busca el último folio de Oaxaca y ajusta el contador"""
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", "oaxaca") \
            .order("folio", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            ultimo_folio = response.data[0]["folio"]
            if isinstance(ultimo_folio, str) and ultimo_folio.startswith(FOLIO_PREFIJO):
                numero = int(ultimo_folio[len(FOLIO_PREFIJO):])
                folio_counter["siguiente"] = numero + 1
                print(f"[OAXACA] Folio inicializado: {ultimo_folio}, siguiente: {folio_counter['siguiente']}")
        else:
            print(f"[OAXACA] No hay folios previos, empezando desde: {folio_counter['siguiente']}")
        
    except Exception as e:
        print(f"[ERROR] Al inicializar folio Oaxaca: {e}")

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ------------ COORDENADAS OAXACA ------------
coords_oaxaca = {
    "folio": (553,96,16,(1,0,0)),
    "fecha1": (168,130,12,(0,0,0)),
    "fecha2": (140,540,10,(0,0,0)),
    "marca": (50,215,12,(0,0,0)),
    "serie": (200,258,12,(0,0,0)),
    "linea": (200,215,12,(0,0,0)),
    "motor": (360,258,12,(0,0,0)),
    "anio": (360,215,12,(0,0,0)),
    "color": (50,258,12,(0,0,0)),
    "vigencia": (410,130,12,(0,0,0)),
    "nombre": (133,149,10,(0,0,0)),
}

# QR dinámico en posición diferente
coords_qr_dinamico = {
    "x": 486,
    "y": 100,
    "ancho": 100,
    "alto": 100
}

coords_oaxaca_segunda = {
    "fecha_exp": (136, 141, 10, (0,0,0)),
    "numero_serie": (136, 166, 10, (0,0,0)),
    "hora": (146, 206, 10, (0,0,0)),
}

# ------------ FUNCIÓN QR DINÁMICO ------------
def generar_qr_dinamico_oaxaca(folio):
    try:
        url_directa = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1
        )
        qr.add_data(url_directa)
        qr.make(fit=True)

        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR OAXACA] Generado para folio {folio} -> {url_directa}")
        return img_qr, url_directa
        
    except Exception as e:
        print(f"[ERROR QR] {e}")
        return None, None

# ------------ GENERACIÓN PDF OAXACA CON QR Y TEXTO ------------
def generar_pdf_oaxaca_completo(folio, datos, fecha_exp, fecha_ven):
    print(f"[OAXACA] Generando PDF para folio: {folio}")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    try:
        doc_original = fitz.open(PLANTILLA_OAXACA)
        pg1 = doc_original[0]
        
        # Fechas
        f1 = fecha_exp.strftime("%d/%m/%Y")
        f_ven = fecha_ven.strftime("%d/%m/%Y")
        
        # INSERTAR TEXTO DEL FOLIO
        pg1.insert_text(coords_oaxaca["folio"][:2], folio, 
                        fontsize=coords_oaxaca["folio"][2], 
                        color=coords_oaxaca["folio"][3])
        
        pg1.insert_text(coords_oaxaca["fecha1"][:2], f1, 
                        fontsize=coords_oaxaca["fecha1"][2], 
                        color=coords_oaxaca["fecha1"][3])
        pg1.insert_text(coords_oaxaca["fecha2"][:2], f1, 
                        fontsize=coords_oaxaca["fecha2"][2], 
                        color=coords_oaxaca["fecha2"][3])

        # Datos del vehículo
        for key in ["marca", "serie", "linea", "motor", "anio", "color"]:
            if key in datos:
                x, y, s, col = coords_oaxaca[key]
                pg1.insert_text((x, y), datos[key], fontsize=s, color=col)

        pg1.insert_text(coords_oaxaca["vigencia"][:2], f_ven, 
                        fontsize=coords_oaxaca["vigencia"][2], 
                        color=coords_oaxaca["vigencia"][3])
        pg1.insert_text(coords_oaxaca["nombre"][:2], datos.get("nombre", ""), 
                        fontsize=coords_oaxaca["nombre"][2], 
                        color=coords_oaxaca["nombre"][3])
        
        # GENERAR E INSERTAR QR DINÁMICO
        img_qr, url_qr = generar_qr_dinamico_oaxaca(folio)
        
        if img_qr:
            buf = BytesIO()
            img_qr.save(buf, format="PNG")
            buf.seek(0)
            qr_pix = fitz.Pixmap(buf.read())

            x_qr = coords_qr_dinamico["x"]
            y_qr = coords_qr_dinamico["y"] 
            ancho_qr = coords_qr_dinamico["ancho"]
            alto_qr = coords_qr_dinamico["alto"]

            pg1.insert_image(
                fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
                pixmap=qr_pix,
                overlay=True
            )
            
            print(f"[OAXACA] QR insertado en ({x_qr}, {y_qr})")
        
        # Segunda plantilla
        doc_segunda = fitz.open(PLANTILLA_OAXACA_SEGUNDA)
        pg2 = doc_segunda[0]
        
        pg2.insert_text(coords_oaxaca_segunda["fecha_exp"][:2], 
                        fecha_exp.strftime("%d/%m/%Y"), 
                        fontsize=coords_oaxaca_segunda["fecha_exp"][2])
        
        pg2.insert_text(coords_oaxaca_segunda["numero_serie"][:2], 
                        datos.get("serie", ""), 
                        fontsize=coords_oaxaca_segunda["numero_serie"][2])
        
        pg2.insert_text(coords_oaxaca_segunda["hora"][:2], 
                        fecha_exp.strftime("%H:%M:%S"), 
                        fontsize=coords_oaxaca_segunda["hora"][2])
        
        # Combinar plantillas
        doc_final = fitz.open()
        doc_final.insert_pdf(doc_original)
        doc_final.insert_pdf(doc_segunda)
        
        salida = os.path.join(OUTPUT_DIR, f"{folio}_oaxaca_completo.pdf")
        doc_final.save(salida)
        
        doc_original.close()
        doc_segunda.close()
        doc_final.close()
        
        return salida
        
    except Exception as e:
        print(f"[ERROR PDF OAXACA] {e}")
        raise

# ------------ HANDLERS OAXACA CON TIMER ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🌮 ¡Órale! Sistema Digital de Permisos OAXACA.\n"
        "Aquí se trabaja en serio y sin mamadas, compadre.\n\n"
        "🚗 Usa /permiso para tramitar tu documento oficial de Oaxaca.\n"
        "💰 Costo: $500 pesos\n"
        "⏰ Tiempo límite para pago: 2 horas\n\n"
        "✨ NOVEDAD: QR dinámico + texto del folio incluidos"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}"
    
    await message.answer(
        f"🚗 TRÁMITE DE PERMISO OAXACA\n\n"
        f"💰 Costo: $500 pesos\n"
        f"⏰ Tiempo para pagar: 2 horas\n"
        f"📱 Concepto de pago: Su folio asignado\n"
        + mensaje_folios + "\n\n"
        "Comenzamos con la MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA: {marca}\n\n"
        "Ahora la LÍNEA del vehículo:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA: {linea}\n\n"
        "El AÑO del vehículo (4 dígitos):"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ El año debe ser de 4 dígitos (ej: 2020).\n"
            "Inténtelo de nuevo:"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ AÑO: {anio}\n\n"
        "NÚMERO DE SERIE del vehículo:"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer(
            "⚠️ El número de serie parece muy corto.\n"
            "Revise bien y escriba el número completo:"
        )
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ SERIE: {serie}\n\n"
        "NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR: {motor}\n\n"
        "COLOR del vehículo:"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ COLOR: {color}\n\n"
        "Por último, el NOMBRE COMPLETO del solicitante:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["folio"] = obtener_siguiente_folio()  # Usar nueva función con persistencia

    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)

    await message.answer(
        f"🔄 PROCESANDO PERMISO OAXACA...\n"
        f"Folio: {datos['folio']}\n"
        f"Titular: {nombre}\n\n"
        "🆕 Generando con QR dinámico + texto del folio..."
    )

    try:
        pdf_path = generar_pdf_oaxaca_completo(datos['folio'], datos, hoy, fecha_ven)

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=f"📋 PERMISO OFICIAL OAXACA\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🔗 QR dinámico + texto incluidos\n"
                   f"💰 Costo: $500 - Tiempo límite: 2 horas"
        )

        # Guardar en base de datos con estado PENDIENTE
        supabase.table("folios_registrados").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "nombre": datos["nombre"],
            "color": datos["color"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": "oaxaca",
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

        # INICIAR TIMER DE PAGO OAXACA
        await iniciar_timer_pago_oaxaca(message.from_user.id, datos['folio'])

        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO OAXACA\n\n"
            f"🌮 Folio: {datos['folio']}\n"
            f"💵 Monto: $500 pesos\n"
            f"⏰ Tiempo límite: 2 horas\n\n"
            
            "🏦 TRANSFERENCIA BANCARIA:\n"
            "• Banco: AZTECA\n"
            "• Titular: ADMINISTRADOR OAXACA\n"
            "• Cuenta: 127180013037579543\n"
            "• Concepto: Permiso " + datos['folio'] + "\n\n"
            
            f"📸 IMPORTANTE: Envíe foto del comprobante de pago.\n"
            f"⚠️ El folio será eliminado automáticamente si no paga en 2 horas.\n\n"
            f"🔗 QR incluido para consulta: {URL_CONSULTA_BASE}/consulta/{datos['folio']}"
        )
        
    except Exception as e:
        await message.answer(f"💥 ERROR: {str(e)}\nIntente con /permiso")
    finally:
        await state.clear()

# ------------ COMPROBANTES DE PAGO OAXACA ------------
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante_oaxaca(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ No tienes folios pendientes de pago en Oaxaca.\n"
            "Para nuevo trámite use /permiso"
        )
        return
    
    if len(folios_usuario) > 1:
        lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
        await message.answer(
            f"📄 MÚLTIPLES FOLIOS OAXACA\n\n"
            f"Tienes {len(folios_usuario)} folios pendientes:\n{lista_folios}\n\n"
            f"Responde con el NÚMERO DE FOLIO para este comprobante."
        )
        return
    
    # Un solo folio
    folio = folios_usuario[0]
    cancelar_timer_folio(folio)
    
    # Actualizar estado
    supabase.table("folios_registrados").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    await message.answer(
        f"✅ COMPROBANTE RECIBIDO OAXACA\n\n"
        f"🌮 Folio: {folio}\n"
        f"📸 Comprobante en revisión\n"
        f"⏰ Timer detenido exitosamente\n\n"
        f"Su permiso será validado pronto."
    )

# ------------ CÓDIGO ADMIN OAXACA ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("OAXACA"))
async def codigo_admin_oaxaca(message: types.Message):
    texto = message.text.strip().upper()
    
    if len(texto) > 6:
        folio_admin = texto[6:]  # Quitar "OAXACA"
        
        if not folio_admin.startswith("1"):
            await message.answer(f"⚠️ Folio Oaxaca inválido: {folio_admin}")
            return
        
        if folio_admin in timers_activos:
            user_con_folio = timers_activos[folio_admin]["user_id"]
            cancelar_timer_folio(folio_admin)
            
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
            
            await message.answer(
                f"✅ TIMER OAXACA DETENIDO\n\n"
                f"🌮 Folio: {folio_admin}\n"
                f"👤 Usuario: {user_con_folio}\n"
                f"📊 Timers restantes: {len(timers_activos)}"
            )
            
            try:
                await bot.send_message(
                    user_con_folio,
                    f"✅ PAGO VALIDADO OAXACA\n\n"
                    f"🌮 Folio: {folio_admin}\n"
                    f"Su permiso ha sido validado por administración.\n"
                    f"Gracias por usar el Sistema Digital Oaxaca."
                )
            except Exception as e:
                print(f"Error notificando usuario Oaxaca {user_con_folio}: {e}")
        else:
            await message.answer(f"❌ Timer no encontrado para folio {folio_admin}")

# ------------ COMANDO FOLIOS ACTIVOS ------------
@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ NO HAY FOLIOS ACTIVOS OAXACA\n\n"
            "No tienes folios pendientes de pago.\n"
            "Para nuevo permiso use /permiso"
        )
        return
    
    lista_folios = []
    for folio in folios_usuario:
        if folio in timers_activos:
            tiempo_restante = 120 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
            tiempo_restante = max(0, tiempo_restante)
            lista_folios.append(f"• {folio} ({tiempo_restante} min restantes)")
        else:
            lista_folios.append(f"• {folio} (sin timer)")
    
    await message.answer(
        f"📋 FOLIOS OAXACA ACTIVOS ({len(folios_usuario)})\n\n"
        + '\n'.join(lista_folios) +
        f"\n\n⏰ Cada folio tiene timer independiente.\n"
        f"📸 Para enviar comprobante, use imagen."
    )

@dp.message()
async def fallback(message: types.Message):
    respuestas_random = [
        "🌮 No entiendo, compadre. Use /permiso para tramitar en Oaxaca.",
        "🚗 Para permisos de Oaxaca use: /permiso",
        "🎯 Sistema Oaxaca con timer: /permiso",
        "🔥 Oaxaca con QR dinámico: /permiso",
    ]
    await message.answer(random.choice(respuestas_random))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    # Inicializar contador de folios desde Supabase
    inicializar_folio_desde_supabase()
    
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
        _keep_task = asyncio.create_task(keep_alive())
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# ------------ ENDPOINTS OAXACA ------------
@app.get("/consulta/{folio}")
async def consulta_folio_directo(folio: str):
    try:
        response = supabase.table("folios_registrados") \
            .select("*") \
            .eq("folio", folio) \
            .eq("entidad", "oaxaca") \
            .execute()
        
        if response.data:
            registro = response.data[0]
            fecha_vencimiento = datetime.fromisoformat(registro["fecha_vencimiento"]).date()
            hoy = datetime.now().date()
            
            estado_visual = "VIGENTE" if fecha_vencimiento >= hoy else "VENCIDO"
            color_estado = "#28a745" if fecha_vencimiento >= hoy else "#dc3545"
            
            html_content = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Folio {folio} - Oaxaca</title><style>
body{{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px}}
.header{{text-align:center;background:white;padding:20px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,0.1);margin-bottom:20px}}
.estado{{background:{color_estado};color:white;padding:15px;text-align:center;font-size:1.2em;font-weight:bold;border-radius:10px;margin:20px 0}}
.info-box{{background:white;padding:20px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,0.1)}}
.info{{margin:15px 0;padding:10px 0;border-bottom:1px solid #eee}}
.label{{font-weight:bold;color:#333;font-size:0.9em}}
.value{{color:#666;margin-top:5px}}
.footer{{text-align:center;margin-top:20px;color:#666;font-size:0.85em}}
.countdown{{background:#fff3cd;border:1px solid #ffeaa7;color:#856404;padding:10px;border-radius:5px;margin:15px 0;text-align:center}}
</style></head><body>
<div class="header">
<h1>Secretaría de Movilidad</h1>
<h2>Gobierno del Estado de Oaxaca</h2>
</div>
<div class="estado">FOLIO {folio} : {estado_visual}</div>
<div class="info-box">
<div class="info"><div class="label">FECHA DE EXPEDICIÓN</div><div class="value">{datetime.fromisoformat(registro['fecha_expedicion']).strftime('%d/%m/%Y')}</div></div>
<div class="info"><div class="label">FECHA DE VENCIMIENTO</div><div class="value">{fecha_vencimiento.strftime('%d/%m/%Y')}</div></div>
<div class="info"><div class="label">MARCA</div><div class="value">{registro['marca']}</div></div>
<div class="info"><div class="label">LÍNEA</div><div class="value">{registro['linea']}</div></div>
<div class="info"><div class="label">AÑO</div><div class="value">{registro['anio']}</div></div>
<div class="info"><div class="label">NÚMERO DE SERIE</div><div class="value">{registro['numero_serie']}</div></div>
<div class="info"><div class="label">NÚMERO DE MOTOR</div><div class="value">{registro['numero_motor']}</div></div>
<div class="info"><div class="label">COLOR</div><div class="value">{registro.get('color', 'N/A')}</div></div>
<div class="info"><div class="label">TITULAR</div><div class="value">{registro.get('nombre', 'N/A')}</div></div>
</div>
<div class="countdown">Actualizando en: <span id="timer">30</span>s</div>
<div class="footer">DOCUMENTO DIGITAL VÁLIDO EN TODO MÉXICO</div>
<script>
let tiempo = 30;
const timer = setInterval(() => {{
    tiempo--;
    document.getElementById('timer').textContent = tiempo;
    if (tiempo <= 0) {{
        window.location.reload();
    }}
}}, 1000);
</script>
</body></html>"""
            
            return HTMLResponse(content=html_content)
        else:
            return HTMLResponse(content=f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>No Encontrado</title></head>
<body style="font-family:Arial;text-align:center;padding:50px;background:#f5f5f5">
<h1>Folio {folio} no encontrado</h1>
<p>El folio no está registrado en Oaxaca.</p>
<a href="/consulta_folio">Consultar otro folio</a>
</body></html>""")
    except Exception as e:
        print(f"[ERROR] Consulta folio {folio}: {e}")
        return HTMLResponse(content=f"<h1>Error del sistema: {str(e)}</h1>")

@app.get("/consulta_folio")
async def consulta_folio_legacy():
    html_redirect = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Sistema de Consulta Oaxaca</title><style>
body{font-family:Arial,sans-serif;background:linear-gradient(135deg,#ff9a9e 0%,#fecfef 100%);margin:0;padding:20px;min-height:100vh;display:flex;align-items:center;justify-content:center}
.container{max-width:450px;background:white;border-radius:20px;box-shadow:0 15px 35px rgba(0,0,0,0.1);padding:40px 30px;text-align:center}
.header{color:#333;margin-bottom:30px}.info{color:#666;line-height:1.6;margin:20px 0}.input-group{margin:25px 0}
.input-group input{width:100%;padding:15px;border:2px solid #ddd;border-radius:10px;font-size:1.1em;text-align:center;letter-spacing:1px}
.btn{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;border:none;padding:15px 30px;border-radius:25px;font-size:1.1em;font-weight:600;cursor:pointer;transition:transform 0.2s}
.btn:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(0,0,0,0.3)}
.note{background:#e3f2fd;padding:15px;border-radius:10px;color:#1976d2;margin:20px 0;font-size:0.9em}
</style></head><body><div class="container"><div class="header"><h1>🏛️ ESTADO DE OAXACA</h1><h2>Consulta de Permiso de Circulación</h2></div>
<div class="info">Ingrese su número de folio para consultar el estado de su permiso:</div>
<div class="input-group"><input type="text" id="folioInput" placeholder="Ejemplo: 1770" maxlength="10"></div>
<button class="btn" onclick="consultarFolio()">🔍 Consultar Estado</button>
<div class="note">💡 Si tiene un permiso con QR, solo escanéelo. Si es anterior, escriba el folio.</div>
</div><script>function consultarFolio(){const folio=document.getElementById('folioInput').value.trim();if(!folio){alert('Por favor ingrese número de folio válido');return;}window.location.href=`/consulta/${folio}`;}
document.getElementById('folioInput').addEventListener('keypress',function(e){if(e.key==='Enter'){consultarFolio();}});</script></body></html>"""
    return HTMLResponse(content=html_redirect)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.get("/")
async def health():
    return {
        "ok": True, 
        "bot": "Oaxaca Permisos con Timer", 
        "status": "running",
        "qr_dinamico": "ACTIVO",
        "timer_sistema": "ACTIVO",
        "folios_persistentes": "ACTIVO",
        "siguiente_folio": f"1{folio_counter['siguiente']}",
        "timers_activos": len(timers_activos),
        "url_consulta": URL_CONSULTA_BASE
    }

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[OAXACA] Servidor iniciando en puerto {port}")
        print(f"[TIMER] Sistema de timers activado")
        print(f"[FOLIOS] Persistencia activada - próximo: 1{folio_counter['siguiente']}")
        print(f"[QR] Dinámico activado: {URL_CONSULTA_BASE}")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] {e}")
