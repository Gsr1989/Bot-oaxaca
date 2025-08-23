from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile
from contextlib import asynccontextmanager, suppress
import asyncio
import qrcode

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_OAXACA = "oaxaca_plantilla_imagen.pdf"
PLANTILLA_OAXACA_SEGUNDA = "oaxacaverga.pdf"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ FOLIO OAXACA ------------
folio_counter = {"count": 86900001}
def nuevo_folio() -> str:
    folio = f"869{folio_counter['count']}"
    folio_counter["count"] += 1
    return folio

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
    # Plantilla original (oaxacachido.pdf)
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

# Coordenadas para la segunda plantilla (oaxacaverga.pdf)
coords_oaxaca_segunda = {
    "fecha_exp": (136, 141, 10, (0,0,0)),
    "numero_serie": (136, 166, 10, (0,0,0)),
    "hora": (146, 206, 10, (0,0,0)),
}

# ------------ GENERACIÓN PDF OAXACA ------------
def generar_pdf_oaxaca_completo(folio, datos, fecha_exp, fecha_ven):
    """
    Genera AMBAS plantillas de Oaxaca en un solo PDF multi-página
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # === PRIMERA PLANTILLA (oaxacachido.pdf) ===
    doc_original = fitz.open(PLANTILLA_OAXACA)
    pg1 = doc_original[0]
    
    # Insertar datos en primera plantilla
    pg1.insert_text(coords_oaxaca["folio"][:2], folio, 
                    fontsize=coords_oaxaca["folio"][2], 
                    color=coords_oaxaca["folio"][3])
    
    f1 = fecha_exp.strftime("%d/%m/%Y")
    f_ven = fecha_ven.strftime("%d/%m/%Y")
    
    pg1.insert_text(coords_oaxaca["fecha1"][:2], f1, 
                    fontsize=coords_oaxaca["fecha1"][2], 
                    color=coords_oaxaca["fecha1"][3])
    pg1.insert_text(coords_oaxaca["fecha2"][:2], f1, 
                    fontsize=coords_oaxaca["fecha2"][2], 
                    color=coords_oaxaca["fecha2"][3])

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

    # --- Generar QR para primera plantilla ---
    texto_qr = f"""FOLIO: {folio}
NOMBRE: {datos.get('nombre', '')}
MARCA: {datos.get('marca', '')}
LINEA: {datos.get('linea', '')}
AÑO: {datos.get('anio', '')}
SERIE: {datos.get('serie', '')}
MOTOR: {datos.get('motor', '')}
COLOR: {datos.get('color', '')}
OAXACA PERMISOS DIGITALES"""

    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=2
    )
    qr.add_data(texto_qr.upper())
    qr.make(fit=True)

    img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img_qr.save(buf, format="PNG")
    buf.seek(0)
    qr_pix = fitz.Pixmap(buf.read())

    # Insertar QR en primera plantilla
    cm = 42.52
    ancho_qr = alto_qr = cm * 1.5
    page_width = pg1.rect.width
    x_qr = page_width - (0.5 * cm) - ancho_qr
    y_qr = 11.5 * cm

    pg1.insert_image(
        fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
        pixmap=qr_pix,
        overlay=True
    )
    
    # === SEGUNDA PLANTILLA (oaxacaverga.pdf) ===
    doc_segunda = fitz.open(PLANTILLA_OAXACA_SEGUNDA)
    pg2 = doc_segunda[0]
    
    # Insertar datos en segunda plantilla
    pg2.insert_text(coords_oaxaca_segunda["fecha_exp"][:2], 
                    fecha_exp.strftime("%d/%m/%Y"), 
                    fontsize=coords_oaxaca_segunda["fecha_exp"][2])
    
    pg2.insert_text(coords_oaxaca_segunda["numero_serie"][:2], 
                    datos.get("serie", ""), 
                    fontsize=coords_oaxaca_segunda["numero_serie"][2])
    
    pg2.insert_text(coords_oaxaca_segunda["hora"][:2], 
                    fecha_exp.strftime("%H:%M:%S"), 
                    fontsize=coords_oaxaca_segunda["hora"][2])
    
    # === COMBINAR AMBAS PLANTILLAS EN UN SOLO PDF ===
    # Crear documento final
    doc_final = fitz.open()
    
    # Insertar primera página (plantilla original)
    doc_final.insert_pdf(doc_original)
    
    # Insertar segunda página (plantilla nueva)
    doc_final.insert_pdf(doc_segunda)
    
    # Guardar el PDF combinado
    salida = os.path.join(OUTPUT_DIR, f"{folio}_oaxaca_completo.pdf")
    doc_final.save(salida)
    
    # Cerrar todos los documentos
    doc_original.close()
    doc_segunda.close()
    doc_final.close()
    
    return salida

# ------------ HANDLERS OAXACA ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🌮 ¡Órale! Sistema Digital de Permisos OAXACA.\n"
        "Aquí se trabaja en serio y sin mamadas, compadre.\n\n"
        "🚗 Usa /permiso para tramitar tu documento oficial de Oaxaca."
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer(
        "🚗 Vamos a generar tu permiso de OAXACA.\n"
        "Primero escribe la MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA: {marca} - Registrado.\n\n"
        "Ahora la LÍNEA del vehículo:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA: {linea} - Anotado.\n\n"
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
        f"✅ AÑO: {anio} - Confirmado.\n\n"
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
        f"✅ SERIE: {serie} - En el sistema.\n\n"
        "NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR: {motor} - Capturado.\n\n"
        "COLOR del vehículo:"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ COLOR: {color} - Registrado.\n\n"
        "Por último, el NOMBRE COMPLETO del solicitante:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["folio"] = nuevo_folio()

    # -------- FECHAS --------
    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)
    # -------------------------

    await message.answer(
        f"🔄 PROCESANDO PERMISO DE OAXACA...\n"
        f"Folio: {datos['folio']}\n"
        f"Titular: {nombre}\n\n"
        "Generando ambas plantillas oficiales..."
    )

    try:
        # Generar PDF con ambas plantillas
        pdf_path = generar_pdf_oaxaca_completo(datos['folio'], datos, hoy, fecha_ven)

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=f"📋 PERMISO OFICIAL OAXACA\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🌮 Documento con ambas plantillas incluidas"
        )

        # Guardar en base de datos
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
        }).execute()

        await message.answer(
            f"🎯 PERMISO DE OAXACA GENERADO EXITOSAMENTE\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"🚗 Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"📅 Vigencia: 30 días\n"
            f"✅ Estado: ACTIVO\n\n"
            "Su documento incluye:\n"
            "• Página 1: Permiso principal con QR\n"
            "• Página 2: Documento de verificación\n\n"
            "Para otro trámite, use /permiso nuevamente."
        )
        
    except Exception as e:
        await message.answer(
            f"💥 ERROR EN EL SISTEMA DE OAXACA\n\n"
            f"Fallo: {str(e)}\n\n"
            "Intente nuevamente con /permiso\n"
            "Si persiste, contacte al administrador."
        )
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    respuestas_random = [
        "🌮 No entiendo, compadre. Use /permiso para tramitar en Oaxaca.",
        "🚗 Para permisos de Oaxaca use: /permiso",
        "🎯 Directo al grano: /permiso para iniciar su trámite oaxaqueño.",
        "🔥 Sistema de Oaxaca: /permiso es lo que necesita.",
    ]
    import random
    await message.answer(random.choice(respuestas_random))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
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

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}
