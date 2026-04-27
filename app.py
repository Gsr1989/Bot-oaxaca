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
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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

URL_CONSULTA_BASE = "https://oaxaca-gob-semovi.onrender.com"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT - 36 HORAS ------------
timers_activos       = {}   # folio -> {task, user_id, start_time, nombre}
user_folios          = {}
pending_comprobantes = {}

# Lock para evitar race condition en generación de folios
_folio_lock = asyncio.Lock()

# ------------ TIMERS ------------
async def eliminar_folio_automatico_oaxaca(folio: str):
    try:
        user_id = timers_activos.get(folio, {}).get("user_id")
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        if user_id:
            await bot.send_message(
                user_id,
                f"TIEMPO AGOTADO - OAXACA\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"Para generar otro permiso use /chuleta"
            )
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio Oaxaca {folio}: {e}")


async def enviar_recordatorio_oaxaca(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos:
            return
        user_id = timers_activos[folio]["user_id"]
        await bot.send_message(
            user_id,
            f"RECORDATORIO DE PAGO - OAXACA\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: $500 pesos\n\n"
            f"Envie su comprobante de pago (imagen) para validar el tramite.\n\n"
            f"Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"Error enviando recordatorio Oaxaca para folio {folio}: {e}")


async def iniciar_timer_pago_oaxaca(user_id: int, folio: str, nombre: str = ""):
    """Inicia timer de 36h guardando el nombre del contribuyente."""
    async def timer_task():
        print(f"[TIMER OAXACA] Iniciado para folio {folio}, usuario {user_id} (36 horas)")

        await asyncio.sleep(34.5 * 3600)

        if folio not in timers_activos: return
        await enviar_recordatorio_oaxaca(folio, 90)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio_oaxaca(folio, 60)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio_oaxaca(folio, 30)
        await asyncio.sleep(20 * 60)

        if folio not in timers_activos: return
        await enviar_recordatorio_oaxaca(folio, 10)
        await asyncio.sleep(10 * 60)

        if folio in timers_activos:
            print(f"[TIMER OAXACA] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico_oaxaca(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task":       task,
        "user_id":    user_id,
        "start_time": datetime.now(),
        "nombre":     nombre,          # ← nombre del contribuyente
    }

    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)

    print(f"[SISTEMA OAXACA] Timer 36h iniciado para folio {folio} ({nombre}), total: {len(timers_activos)}")


def cancelar_timer_folio(folio: str):
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
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]


def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ------------ FOLIO OAXACA CON PREFIJO "1" ------------
FOLIO_PREFIJO = "1"
folio_counter = {"siguiente": 670}
MAX_INTENTOS_FOLIO = 100_000


def _obtener_siguiente_folio_sync() -> str:
    """Síncrono — se llama siempre dentro de _folio_lock."""
    for _ in range(MAX_INTENTOS_FOLIO):
        folio_num = folio_counter["siguiente"]
        folio     = f"{FOLIO_PREFIJO}{folio_num}"
        try:
            response = supabase.table("folios_registrados") \
                .select("folio").eq("folio", folio).execute()
            if not response.data:
                folio_counter["siguiente"] += 1
                print(f"[FOLIO OAXACA] Asignado: {folio}")
                return folio
            else:
                print(f"[FOLIO OAXACA] {folio} ya existe, buscando siguiente...")
                folio_counter["siguiente"] += 1
        except Exception as e:
            print(f"[ERROR FOLIO OAXACA] {e}")
            folio_counter["siguiente"] += 1

    raise Exception(f"No se pudo generar folio único después de {MAX_INTENTOS_FOLIO} intentos")


async def obtener_siguiente_folio() -> str:
    """Async con Lock — evita race condition en requests simultáneos."""
    async with _folio_lock:
        return await asyncio.to_thread(_obtener_siguiente_folio_sync)


def inicializar_folio_desde_supabase():
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .eq("entidad", "oaxaca") \
            .like("folio", f"{FOLIO_PREFIJO}%") \
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
            print(f"[OAXACA] Sin folios previos, empezando desde: {FOLIO_PREFIJO}{folio_counter['siguiente']}")
    except Exception as e:
        print(f"[ERROR] Al inicializar folio Oaxaca: {e}")

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    color  = State()
    nombre = State()

# ------------ COORDENADAS OAXACA ------------
coords_oaxaca = {
    "folio":    (553, 96,  16, (1, 0, 0)),
    "fecha1":   (168, 130, 12, (0, 0, 0)),
    "fecha2":   (140, 540, 10, (0, 0, 0)),
    "marca":    (50,  215, 12, (0, 0, 0)),
    "serie":    (200, 258, 12, (0, 0, 0)),
    "linea":    (200, 215, 12, (0, 0, 0)),
    "motor":    (360, 258, 12, (0, 0, 0)),
    "anio":     (360, 215, 12, (0, 0, 0)),
    "color":    (50,  258, 12, (0, 0, 0)),
    "vigencia": (410, 130, 12, (0, 0, 0)),
    "nombre":   (133, 149, 10, (0, 0, 0)),
}

coords_qr_dinamico = {"x": 486, "y": 100, "ancho": 100, "alto": 100}

coords_oaxaca_segunda = {
    "fecha_exp":    (136, 141, 10, (0, 0, 0)),
    "numero_serie": (136, 166, 10, (0, 0, 0)),
    "hora":         (146, 206, 10, (0, 0, 0)),
}

# ------------ QR DINÁMICO ------------
def generar_qr_dinamico_oaxaca(folio):
    try:
        url = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                             box_size=4, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR OAXACA] {folio} -> {url}")
        return img, url
    except Exception as e:
        print(f"[ERROR QR] {e}")
        return None, None

# ------------ PDF OAXACA UNIFICADO ------------
def generar_pdf_oaxaca_completo(folio, datos, fecha_exp, fecha_ven):
    print(f"[OAXACA] Generando PDF UNIFICADO para folio: {folio}")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    doc_original = fitz.open(PLANTILLA_OAXACA)
    pg1 = doc_original[0]

    f1    = fecha_exp.strftime("%d/%m/%Y")
    f_ven = fecha_ven.strftime("%d/%m/%Y")

    pg1.insert_text(coords_oaxaca["folio"][:2],    folio, fontsize=coords_oaxaca["folio"][2],    color=coords_oaxaca["folio"][3])
    pg1.insert_text(coords_oaxaca["fecha1"][:2],   f1,    fontsize=coords_oaxaca["fecha1"][2],   color=coords_oaxaca["fecha1"][3])
    pg1.insert_text(coords_oaxaca["fecha2"][:2],   f1,    fontsize=coords_oaxaca["fecha2"][2],   color=coords_oaxaca["fecha2"][3])
    pg1.insert_text(coords_oaxaca["vigencia"][:2], f_ven, fontsize=coords_oaxaca["vigencia"][2], color=coords_oaxaca["vigencia"][3])
    pg1.insert_text(coords_oaxaca["nombre"][:2],   datos.get("nombre", ""), fontsize=coords_oaxaca["nombre"][2], color=coords_oaxaca["nombre"][3])

    for key in ["marca", "serie", "linea", "motor", "anio", "color"]:
        if key in datos:
            x, y, s, col = coords_oaxaca[key]
            pg1.insert_text((x, y), datos[key], fontsize=s, color=col)

    img_qr, _ = generar_qr_dinamico_oaxaca(folio)
    if img_qr:
        buf = BytesIO()
        img_qr.save(buf, format="PNG")
        buf.seek(0)
        qr_pix = fitz.Pixmap(buf.read())
        x_qr, y_qr = coords_qr_dinamico["x"], coords_qr_dinamico["y"]
        w_qr, h_qr = coords_qr_dinamico["ancho"], coords_qr_dinamico["alto"]
        pg1.insert_image(fitz.Rect(x_qr, y_qr, x_qr+w_qr, y_qr+h_qr), pixmap=qr_pix, overlay=True)
        print(f"[OAXACA] QR insertado en ({x_qr}, {y_qr})")

    doc_segunda = fitz.open(PLANTILLA_OAXACA_SEGUNDA)
    pg2 = doc_segunda[0]
    pg2.insert_text(coords_oaxaca_segunda["fecha_exp"][:2],    fecha_exp.strftime("%d/%m/%Y"), fontsize=coords_oaxaca_segunda["fecha_exp"][2],    color=coords_oaxaca_segunda["fecha_exp"][3])
    pg2.insert_text(coords_oaxaca_segunda["numero_serie"][:2], datos.get("serie", ""),         fontsize=coords_oaxaca_segunda["numero_serie"][2], color=coords_oaxaca_segunda["numero_serie"][3])
    pg2.insert_text(coords_oaxaca_segunda["hora"][:2],         fecha_exp.strftime("%H:%M:%S"), fontsize=coords_oaxaca_segunda["hora"][2],         color=coords_oaxaca_segunda["hora"][3])

    doc_final = fitz.open()
    doc_final.insert_pdf(doc_original, from_page=0, to_page=0)
    doc_final.insert_pdf(doc_segunda,  from_page=0, to_page=0)

    salida = os.path.join(OUTPUT_DIR, f"{folio}_oaxaca_completo.pdf")
    doc_final.save(salida)

    doc_original.close()
    doc_segunda.close()
    doc_final.close()

    print(f"[OAXACA] PDF unificado generado: {salida}")
    return salida

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Sistema Digital de Permisos OAXACA\n"
        "Servicio oficial automatizado para tramites vehiculares\n\n"
        "Costo: $500 pesos\n"
        "Tiempo limite para pago: 36 horas\n\n"
        "PDF unificado con QR dinamico + folio"
    )


@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    await state.clear()

    mis_folios = [f for f in timers_activos
                  if timers_activos[f].get("user_id") == message.from_user.id]

    if mis_folios:
        texto   = "FOLIOS ACTIVOS CON TIMER\n" + "─" * 28 + "\n\n"
        botones = []
        for f in mis_folios:
            info   = timers_activos[f]
            nombre = info.get("nombre", "Sin nombre")
            mins   = max(0, 2160 - int((datetime.now() - info["start_time"]).total_seconds() / 60))
            texto += f"Folio: {f}\n{nombre}\n{mins//60}h {mins%60}min restantes\n\n"
            botones.append([
                InlineKeyboardButton(
                    text=f"Detener timer {f}",
                    callback_data=f"detener_{f}"
                )
            ])
        await message.answer(texto.strip(), reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))
        await message.answer(
            f"Para NUEVO permiso escribe la MARCA del vehiculo:\n\nCosto: $500 | Plazo: 36h")
    else:
        await message.answer(
            f"TRAMITE DE PERMISO OAXACA\n\n"
            f"Costo: $500 pesos\n"
            f"Tiempo para pagar: 36 horas\n\n"
            "Comenzamos con la MARCA del vehiculo:")

    await state.set_state(PermisoForm.marca)


@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LINEA del vehiculo:")
    await state.set_state(PermisoForm.linea)


@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("ANO del vehiculo (4 digitos):")
    await state.set_state(PermisoForm.anio)


@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("El ano debe ser de 4 digitos (ej: 2020):")
        return
    await state.update_data(anio=anio)
    await message.answer("NUMERO DE SERIE del vehiculo:")
    await state.set_state(PermisoForm.serie)


@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("NUMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)


@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("COLOR del vehiculo:")
    await state.set_state(PermisoForm.color)


@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del solicitante:")
    await state.set_state(PermisoForm.nombre)


@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos  = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre

    # Genera folio con Lock
    try:
        folio = await obtener_siguiente_folio()
    except Exception as e:
        await message.answer(f"ERROR generando folio: {str(e)}\n\nPara generar otro permiso use /chuleta")
        await state.clear()
        return

    hoy       = datetime.now()
    fecha_ven = hoy + timedelta(days=30)

    await message.answer(
        f"PROCESANDO PERMISO OAXACA...\n"
        f"Folio: {folio}\n"
        f"Titular: {nombre}\n\n"
        "Generando PDF unificado con QR dinamico..."
    )

    try:
        pdf_path = await asyncio.to_thread(generar_pdf_oaxaca_completo, folio, datos, hoy, fecha_ven)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Validar Admin",  callback_data=f"validar_{folio}"),
            InlineKeyboardButton(text="Detener Timer",  callback_data=f"detener_{folio}")
        ]])

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=(
                f"PERMISO OFICIAL OAXACA\n"
                f"Folio: {folio}\n"
                f"Titular: {nombre}\n"
                f"Vigencia: 30 dias\n"
                f"PDF unificado (2 paginas)\n"
                f"QR dinamico incluido\n\n"
                f"TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        # INSERT con reintento en duplicate key
        folio_final = folio

        def _insert(folio_usar: str):
            supabase.table("folios_registrados").insert({
                "folio":             folio_usar,
                "marca":             datos["marca"],
                "linea":             datos["linea"],
                "anio":              datos["anio"],
                "numero_serie":      datos["serie"],
                "numero_motor":      datos["motor"],
                "nombre":            nombre,
                "color":             datos["color"],
                "fecha_expedicion":  hoy.date().isoformat(),
                "fecha_vencimiento": fecha_ven.date().isoformat(),
                "entidad":           "oaxaca",
                "estado":            "PENDIENTE",
                "user_id":           message.from_user.id,
                "username":          message.from_user.username or "Sin username"
            }).execute()

        for _ in range(20):
            try:
                await asyncio.to_thread(_insert, folio_final)
                folio = folio_final
                print(f"[DB] Insertado folio {folio_final}")
                break
            except Exception as e:
                em = str(e).lower()
                if any(k in em for k in ("duplicate", "unique", "23505")):
                    print(f"[DB] Folio {folio_final} duplicado — obteniendo nuevo...")
                    folio_final = await obtener_siguiente_folio()
                else:
                    print(f"[DB ERROR] {e}")
                    break

        await iniciar_timer_pago_oaxaca(message.from_user.id, folio, nombre)

        await message.answer(
            f"INSTRUCCIONES DE PAGO OAXACA\n\n"
            f"Folio: {folio}\n"
            f"Monto: $500 pesos\n"
            f"Tiempo limite: 36 HORAS\n\n"
            f"TRANSFERENCIA BANCARIA:\n"
            f"Banco: AZTECA\n"
            f"Titular: ADMINISTRADOR OAXACA\n"
            f"Cuenta: 127180013037579543\n"
            f"Concepto: Permiso {folio}\n\n"
            f"Envie foto del comprobante de pago.\n"
            f"El folio sera eliminado si no paga en 36 horas.\n\n"
            f"Para generar otro permiso use /chuleta\n"
            f"QR incluido para consulta: {URL_CONSULTA_BASE}/consulta/{folio}"
        )

    except Exception as e:
        await message.answer(f"ERROR: {str(e)}\n\nPara generar otro permiso use /chuleta")
    finally:
        await state.clear()


# ------------ CALLBACK HANDLERS ------------
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")

    if not folio.startswith("1"):
        await callback.answer("Folio invalido", show_alert=True)
        return

    if folio in timers_activos:
        user_con_folio = timers_activos[folio]["user_id"]
        nombre         = timers_activos[folio].get("nombre", "")
        cancelar_timer_folio(folio)

        try:
            supabase.table("folios_registrados").update({
                "estado":           "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")

        await callback.answer("Folio validado por administracion", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)

        try:
            await bot.send_message(
                user_con_folio,
                f"PAGO VALIDADO POR ADMINISTRACION - OAXACA\n"
                f"Folio: {folio}\n"
                f"Titular: {nombre}\n"
                f"Tu permiso esta activo para circular.\n\n"
                f"Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando al usuario {user_con_folio}: {e}")
    else:
        await callback.answer("Folio no encontrado en timers activos", show_alert=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")

    if folio in timers_activos:
        nombre = timers_activos[folio].get("nombre", "")
        cancelar_timer_folio(folio)

        try:
            supabase.table("folios_registrados").update({
                "estado":          "TIMER_DETENIDO",
                "fecha_detencion": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")

        await callback.answer("Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"TIMER DETENIDO\n"
            f"Folio: {folio}\n"
            f"Titular: {nombre}\n\n"
            f"El folio ya NO se eliminara automaticamente.\n\n"
            f"Para generar otro permiso use /chuleta"
        )
    else:
        await callback.answer("Timer ya no esta activo", show_alert=True)


# ------------ CÓDIGO ADMIN SERO ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def codigo_admin_sero(message: types.Message):
    texto = message.text.strip().upper()

    if len(texto) <= 4:
        await message.answer(
            "FORMATO INCORRECTO\n\n"
            "Use el formato: SERO[numero de folio]\n"
            "Ejemplo: SERO1770\n\n"
            "Para generar otro permiso use /chuleta"
        )
        return

    folio_admin = texto[4:]

    if not folio_admin.startswith("1"):
        await message.answer(
            f"FOLIO INVALIDO\n\n"
            f"El folio {folio_admin} no es un folio OAXACA valido.\n"
            f"Los folios de OAXACA deben comenzar con 1.\n\n"
            f"Ejemplo correcto: SERO1770\n\n"
            f"Para generar otro permiso use /chuleta"
        )
        return

    if folio_admin in timers_activos:
        user_con_folio = timers_activos[folio_admin]["user_id"]
        nombre         = timers_activos[folio_admin].get("nombre", "")
        cancelar_timer_folio(folio_admin)

        try:
            supabase.table("folios_registrados").update({
                "estado":           "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio_admin).execute()
        except Exception as e:
            print(f"Error actualizando estado admin: {e}")

        await message.answer(
            f"VALIDACION ADMINISTRATIVA OK\n"
            f"Folio: {folio_admin}\n"
            f"Titular: {nombre}\n"
            f"Timer cancelado y estado actualizado.\n"
            f"Timers restantes: {len(timers_activos)}\n\n"
            f"Para generar otro permiso use /chuleta"
        )

        try:
            await bot.send_message(
                user_con_folio,
                f"PAGO VALIDADO POR ADMINISTRACION - OAXACA\n"
                f"Folio: {folio_admin}\n"
                f"Tu permiso esta activo para circular.\n\n"
                f"Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando usuario Oaxaca {user_con_folio}: {e}")
    else:
        await message.answer(
            f"FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\n"
            f"Folio consultado: {folio_admin}\n\n"
            f"Para generar otro permiso use /chuleta"
        )


# ------------ COMPROBANTE FOTO ------------
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante_oaxaca(message: types.Message):
    user_id        = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)

    if not folios_usuario:
        await message.answer(
            "No tienes folios pendientes de pago en Oaxaca.\n\n"
            "Para generar otro permiso use /chuleta"
        )
        return

    if len(folios_usuario) > 1:
        lista = '\n'.join([f"- {folio}" for folio in folios_usuario])
        pending_comprobantes[user_id] = "waiting_folio"
        await message.answer(
            f"MULTIPLES FOLIOS OAXACA\n\n"
            f"Tienes {len(folios_usuario)} folios pendientes:\n{lista}\n\n"
            f"Responde con el NUMERO DE FOLIO para este comprobante.\n\n"
            f"Para generar otro permiso use /chuleta"
        )
        return

    folio = folios_usuario[0]
    cancelar_timer_folio(folio)

    try:
        supabase.table("folios_registrados").update({
            "estado":           "COMPROBANTE_ENVIADO",
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute()
    except Exception as e:
        print(f"Error actualizando estado: {e}")

    await message.answer(
        f"COMPROBANTE RECIBIDO CORRECTAMENTE\n\n"
        f"Folio: {folio}\n"
        f"Comprobante en revision\n"
        f"Timer detenido exitosamente\n\n"
        f"Su permiso sera validado pronto.\n\n"
        f"Para generar otro permiso use /chuleta"
    )


@dp.message(lambda message: message.from_user.id in pending_comprobantes
            and pending_comprobantes[message.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    user_id            = message.from_user.id
    folio_especificado = message.text.strip().upper()
    folios_usuario     = obtener_folios_usuario(user_id)

    if folio_especificado not in folios_usuario:
        await message.answer(
            "Ese folio no esta entre tus expedientes activos.\n"
            "Responde con uno de tu lista actual.\n\n"
            "Para generar otro permiso use /chuleta"
        )
        return

    cancelar_timer_folio(folio_especificado)
    del pending_comprobantes[user_id]

    try:
        supabase.table("folios_registrados").update({
            "estado":           "COMPROBANTE_ENVIADO",
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio_especificado).execute()
    except Exception as e:
        print(f"Error actualizando estado: {e}")

    await message.answer(
        f"Comprobante asociado.\n"
        f"Folio: {folio_especificado}\n"
        f"Timer detenido.\n\n"
        f"Para generar otro permiso use /chuleta"
    )


@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    user_id        = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)

    if not folios_usuario:
        await message.answer(
            "NO HAY FOLIOS ACTIVOS OAXACA\n\n"
            "No tienes folios pendientes de pago.\n\n"
            "Para generar otro permiso use /chuleta"
        )
        return

    lista_folios = []
    for folio in folios_usuario:
        if folio in timers_activos:
            info   = timers_activos[folio]
            nombre = info.get("nombre", "Sin nombre")
            mins   = max(0, 2160 - int((datetime.now() - info["start_time"]).total_seconds() / 60))
            lista_folios.append(f"- {folio} — {nombre}\n  {mins//60}h {mins%60}min restantes")
        else:
            lista_folios.append(f"- {folio} (sin timer)")

    await message.answer(
        f"FOLIOS OAXACA ACTIVOS ({len(folios_usuario)})\n\n"
        + '\n\n'.join(lista_folios) +
        f"\n\nCada folio tiene timer de 36 horas.\n"
        f"Para enviar comprobante usa imagen.\n\n"
        f"Para generar otro permiso use /chuleta"
    )


@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        f"INFORMACION DE COSTO\n\n"
        f"El costo del permiso es $500 pesos.\n\n"
        "Para generar otro permiso use /chuleta"
    )


@dp.message()
async def fallback(message: types.Message):
    await message.answer("Sistema Digital Oaxaca.")


# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    inicializar_folio_desde_supabase()

    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message", "callback_query"])
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
            .select("*").eq("folio", folio).eq("entidad", "oaxaca").execute()

        if response.data:
            registro         = response.data[0]
            fecha_vencimiento = datetime.fromisoformat(registro["fecha_vencimiento"]).date()
            hoy              = datetime.now().date()
            estado_visual    = "VIGENTE" if fecha_vencimiento >= hoy else "VENCIDO"
            color_estado     = "#28a745" if fecha_vencimiento >= hoy else "#dc3545"

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
<div class="header"><h1>Secretaria de Movilidad</h1><h2>Gobierno del Estado de Oaxaca</h2></div>
<div class="estado">FOLIO {folio} : {estado_visual}</div>
<div class="info-box">
<div class="info"><div class="label">FECHA DE EXPEDICION</div><div class="value">{datetime.fromisoformat(registro['fecha_expedicion']).strftime('%d/%m/%Y')}</div></div>
<div class="info"><div class="label">FECHA DE VENCIMIENTO</div><div class="value">{fecha_vencimiento.strftime('%d/%m/%Y')}</div></div>
<div class="info"><div class="label">MARCA</div><div class="value">{registro['marca']}</div></div>
<div class="info"><div class="label">LINEA</div><div class="value">{registro['linea']}</div></div>
<div class="info"><div class="label">ANO</div><div class="value">{registro['anio']}</div></div>
<div class="info"><div class="label">NUMERO DE SERIE</div><div class="value">{registro['numero_serie']}</div></div>
<div class="info"><div class="label">NUMERO DE MOTOR</div><div class="value">{registro['numero_motor']}</div></div>
<div class="info"><div class="label">COLOR</div><div class="value">{registro.get('color', 'N/A')}</div></div>
<div class="info"><div class="label">TITULAR</div><div class="value">{registro.get('nombre', 'N/A')}</div></div>
</div>
<div class="countdown">Actualizando en: <span id="timer">30</span>s</div>
<div class="footer">DOCUMENTO DIGITAL VALIDO EN TODO MEXICO</div>
<script>
let tiempo = 30;
const t = setInterval(() => {{
    tiempo--;
    document.getElementById('timer').textContent = tiempo;
    if (tiempo <= 0) window.location.reload();
}}, 1000);
</script>
</body></html>"""
            return HTMLResponse(content=html_content)

        else:
            return HTMLResponse(content=f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>No Encontrado</title></head>
<body style="font-family:Arial;text-align:center;padding:50px;background:#f5f5f5">
<h1>Folio {folio} no encontrado</h1><p>El folio no esta registrado en Oaxaca.</p>
<a href="/consulta_folio">Consultar otro folio</a></body></html>""")

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
</style></head><body><div class="container"><div class="header"><h1>ESTADO DE OAXACA</h1><h2>Consulta de Permiso de Circulacion</h2></div>
<div class="info">Ingrese su numero de folio para consultar el estado de su permiso:</div>
<div class="input-group"><input type="text" id="folioInput" placeholder="Ejemplo: 1770" maxlength="10"></div>
<button class="btn" onclick="consultarFolio()">Consultar Estado</button>
<div class="note">Si tiene un permiso con QR, solo escanéelo. Si es anterior, escriba el folio.</div>
</div><script>function consultarFolio(){const folio=document.getElementById('folioInput').value.trim();if(!folio){alert('Por favor ingrese numero de folio valido');return;}window.location.href=`/consulta/${folio}`;}
document.getElementById('folioInput').addEventListener('keypress',function(e){if(e.key==='Enter'){consultarFolio();}});</script></body></html>"""
    return HTMLResponse(content=html_redirect)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data   = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}


@app.get("/")
async def health():
    return {
        "ok":              True,
        "bot":             "Oaxaca Permisos Sistema",
        "status":          "running",
        "version":         "5.1",
        "pdf_unificado":   "ACTIVO",
        "qr_dinamico":     "ACTIVO",
        "timer_sistema":   "36 HORAS",
        "siguiente_folio": f"{FOLIO_PREFIJO}{folio_counter['siguiente']}",
        "timers_activos":  len(timers_activos),
        "url_consulta":    URL_CONSULTA_BASE,
        "fixes_v5.1": [
            "asyncio.Lock en obtener_siguiente_folio — elimina race condition",
            "INSERT con retry en duplicate key — reintenta hasta 20 veces",
            "/chuleta muestra folios activos con nombre + boton detener timer",
            "Timer guarda nombre del contribuyente",
            "generar_pdf_oaxaca_completo con asyncio.to_thread",
        ]
    }


if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[OAXACA] Servidor iniciando en puerto {port}")
        print(f"[FOLIO] Proximo disponible: {FOLIO_PREFIJO}{folio_counter['siguiente']}")
        print(f"[QR] Dinamico activado: {URL_CONSULTA_BASE}")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] {e}")
