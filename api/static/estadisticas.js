"use strict";
const $ = id => document.getElementById(id);
let datosActuales = null;
let facturaActual = null;
let peticionCarga = 0;
const fechaLocal = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
const filtros = () => ({desde: $("desde").value, hasta: $("hasta").value, moneda: $("moneda").value});
const dinero = (valor, moneda = filtros().moneda) => valor == null ? "Sin total" : new Intl.NumberFormat("es-CL", {style:"currency",currency:moneda,maximumFractionDigits:moneda === "CLP" ? 0 : 2}).format(valor);
const elemento = (tag, clase, texto) => {const e=document.createElement(tag); if(clase)e.className=clase; if(texto!=null)e.textContent=texto; return e;};
function periodoInicial() {
  const hoy = new Date();
  $("desde").value = fechaLocal(new Date(hoy.getFullYear(),hoy.getMonth()-2,1));
  $("hasta").value = fechaLocal(hoy);
}
async function consultar(url, opciones) {
  const respuesta = await fetch(url, opciones);
  let datos;
  try {datos = await respuesta.json();} catch {throw new Error("El servidor devolvió una respuesta inválida.");}
  if(!respuesta.ok) throw new Error(typeof datos.detail === "string" ? datos.detail : "Revisa los campos de la solicitud.");
  return datos;
}
function mostrarProveedores(grupos) {
  const cont = $("proveedores"); cont.replaceChildren();
  $("cantidad-proveedores").textContent = `${grupos.length} proveedores`;
  if(!grupos.length) {cont.append(elemento("p","empty","No hay compras validadas en este período.")); return;}
  const maximo = Math.max(...grupos.map(p=>Number(p.total)),1);
  for(const p of grupos) {
    const fila=elemento("div","provider-row"), etiqueta=elemento("div","provider-label");
    etiqueta.append(elemento("span","",p.proveedor),elemento("strong","",dinero(p.total)));
    const track=elemento("div","track"),barra=elemento("div","fill"); barra.style.width=`${Number(p.total)/maximo*100}%`;
    track.append(barra);fila.append(etiqueta,track);cont.append(fila);
  }
}
function mostrarProductos(grupos, cobertura) {
  const cont=$("productos");cont.replaceChildren();
  const limite=Number($("limite-productos").value);
  const visibles=limite?grupos.slice(0,limite):grupos;
  if(!visibles.length)cont.append(elemento("p","empty","No hay líneas con importes suficientes para comparar productos."));
  const maximo=Math.max(...grupos.map(p=>Number(p.total)),1);
  for(const p of visibles) {
    const fila=elemento("div","provider-row"), etiqueta=elemento("div","provider-label");
    etiqueta.append(elemento("span","",p.producto),elemento("strong","",`${dinero(p.total)}${p.estimado?" *":""}`));
    const track=elemento("div","track"),barra=elemento("div","fill product-fill");barra.style.width=`${Number(p.total)/maximo*100}%`;
    track.append(barra);
    const detalle=`${p.sku||"Sin SKU"}${p.formato?" · "+p.formato:""} · ${p.facturas} factura(s) · ${p.lineas} línea(s)${p.estimado?" · Incluye estimaciones por cantidad × precio registrado":" · Importe neto de línea registrado"}`;
    fila.setAttribute("aria-label",`${p.producto}: ${dinero(p.total)}. ${detalle}`);
    fila.append(etiqueta,track,elemento("small","muted",detalle));cont.append(fila);
  }
  $("cobertura-productos").textContent=`${visibles.length} de ${grupos.length} productos · ${cobertura.lineas_incluidas} líneas incluidas · ${cobertura.lineas_omitidas} omitidas por datos incompletos o packs ambiguos.${cobertura.lineas_estimadas?" * Hay "+cobertura.lineas_estimadas+" líneas estimadas; pueden no reflejar descuentos de la factura.":""} Este desglose puede no coincidir con el total de compras.`;
}
$("limite-productos").addEventListener("change",()=>{if(datosActuales)mostrarProductos(datosActuales.por_producto,datosActuales.cobertura_productos);});
function mostrarMeses(grupos) {
  const cont=$("meses");cont.replaceChildren();
  const maximo=Math.max(...grupos.map(m=>Number(m.total)),1);
  for(const m of grupos) {
    const columna=elemento("div","month");
    const fecha=new Date(`${m.mes}-01T12:00:00`);
    const label=fecha.toLocaleDateString("es-CL",{month:"short",year:"2-digit"});
    columna.setAttribute("aria-label",`${label}: ${dinero(m.total)}`);
    const barra=elemento("div","month-bar");barra.style.height=`${Number(m.total)/maximo*155}px`;
    columna.append(elemento("span","month-value",dinero(m.total)),barra,elemento("span","month-name",label));cont.append(columna);
  }
}
function mostrarFacturas(cont, facturas) {
  cont.replaceChildren();
  if(!facturas.length){cont.append(elemento("p","empty","No hay facturas para mostrar."));return;}
  const lista=elemento("div","record-list");
  for(const f of facturas) {
    const fila=elemento("div","record"), texto=elemento("div");
    texto.append(elemento("div","record-title",`${f.folio} · ${f.proveedor}`),elemento("small","",f.fecha_emision || "Fecha de emisión desconocida"));
    const etiqueta=f.requiere_revision ? "Por revisar" : "Encabezado validado";
    texto.append(elemento("small",f.requiere_revision ? "pending":"",etiqueta));
    const boton=elemento("button","","Revisar");boton.type="button";boton.addEventListener("click",()=>abrirFactura(f));
    fila.append(texto,elemento("span","amount",dinero(f.total_factura,f.moneda || filtros().moneda)),boton);lista.append(fila);
  }
  cont.append(lista);
}
async function cargar() {
  const numero=++peticionCarga;
  const f=filtros();
  if(!f.desde||!f.hasta||f.desde>f.hasta){$("estado-carga").textContent="Selecciona un rango de fechas válido.";return;}
  $("estado-carga").className="status";$("estado-carga").textContent="Consultando historial…";
  $("exportar").setAttribute("aria-disabled","true");
  try {
    const datos=await consultar(`/api/estadisticas?${new URLSearchParams(f)}`);
    if(numero!==peticionCarga)return;
    datosActuales=datos;
    $("gasto").textContent=dinero(datos.resumen.gasto_validado);
    $("procesadas").textContent=datos.resumen.facturas_procesadas;
    $("pendientes").textContent=datos.resumen.facturas_por_revisar;
    mostrarProveedores(datos.por_proveedor);mostrarMeses(datos.por_mes);mostrarFacturas($("facturas"),datos.facturas);mostrarFacturas($("sin-fecha"),datos.sin_fecha);
    mostrarProductos(datos.por_producto,datos.cobertura_productos);
    $("sin-fecha-panel").hidden=!datos.sin_fecha.length;
    const r=datos.resumen;
    $("aviso").hidden=!(r.facturas_sin_datos_suficientes||r.facturas_sin_fecha_global);
    $("aviso").textContent=`${r.facturas_sin_datos_suficientes} facturas del período tienen datos financieros pendientes y no se suman. ${r.facturas_sin_fecha_global} facturas del historial general no tienen fecha y se muestran aparte.`;
    $("exportar").href=`/api/estadisticas/exportar.csv?${new URLSearchParams(f)}`;$("exportar").setAttribute("aria-disabled","false");
    $("estado-carga").textContent=`Datos actualizados · ${f.desde} a ${f.hasta} · ${f.moneda}`;
  } catch(error) {
    if(numero!==peticionCarga)return;
    datosActuales=null;$("estado-carga").className="status error";$("estado-carga").textContent=error.message;
    for(const id of ["gasto","procesadas","pendientes"])$(id).textContent="—";
    for(const id of ["proveedores","meses","facturas","sin-fecha","productos","cobertura-productos"])$(id).replaceChildren();
    $("aviso").hidden=true;$("sin-fecha-panel").hidden=true;
  }
}
function agregarImpuesto(impuesto = {}) {
  const fila=elemento("div","tax-row");
  for(const [campo,titulo] of [["nombre","Descripción"],["codigo","Código"],["tasa","Tasa impresa"],["monto","Monto"]]) {
    const etiqueta=elemento("label","",titulo),input=elemento("input",`tax-${campo}`);
    input.value=impuesto[campo]??"";
    if(campo==="monto"){input.type="number";input.min="0";input.step="0.01";}
    else input.maxLength=campo==="nombre"?200:30;
    input.addEventListener("input",actualizarImpuestos);etiqueta.append(input);fila.append(etiqueta);
  }
  const quitar=elemento("button","button secondary","Quitar");quitar.type="button";
  quitar.addEventListener("click",()=>{fila.remove();actualizarImpuestos();});fila.append(quitar);
  $("impuestos-adicionales").append(fila);actualizarImpuestos();
}
function leerImpuestos() {
  return [...$("impuestos-adicionales").querySelectorAll(".tax-row")].map(fila=>Object.fromEntries(["nombre","codigo","tasa","monto"].map(c=>[c,fila.querySelector(`.tax-${c}`).value.trim()||null])));
}
function actualizarImpuestos() {
  const activo=$("usar-desglose").checked,formulario=$("finanzas-form");
  $("desglose-impuestos").hidden=!activo;
  formulario.elements.monto_impuestos.readOnly=activo;
  if(activo) {
    const iva=formulario.elements.monto_iva.value,adicionales=leerImpuestos();
    formulario.elements.monto_impuestos.value=iva!==""&&adicionales.every(i=>i.monto!==null)?(Number(iva)+adicionales.reduce((s,i)=>s+Number(i.monto),0)).toFixed(2):"";
  }
}
$("usar-desglose").addEventListener("change",actualizarImpuestos);
$("finanzas-form").elements.monto_iva.addEventListener("input",actualizarImpuestos);
$("agregar-impuesto").addEventListener("click",()=>agregarImpuesto());
async function abrirFactura(f) {
  facturaActual=f;
  $("detalle-titulo").textContent=`Factura ${f.folio}`;
  $("detalle-estado").textContent=`${f.proveedor} · ${f.estado}`;
  $("motivos").replaceChildren(...(f.motivos_revision||[]).map(m=>elemento("li","",m.mensaje)));
  $("guardar-estado").textContent="";
  const formulario=$("finanzas-form");
  for(const nombre of ["proveedor","rut_proveedor","fecha_emision","moneda","total_factura","monto_neto","monto_impuestos","monto_exento","monto_iva"])formulario.elements[nombre].value=f[nombre]??"";
  $("usar-desglose").checked=f.monto_iva!=null||Array.isArray(f.impuestos_adicionales);
  $("impuestos-adicionales").replaceChildren();
  for(const impuesto of f.impuestos_adicionales||[])agregarImpuesto(impuesto);
  actualizarImpuestos();
  $("imagen").hidden=true;$("imagen").removeAttribute("href");
  if(!$("detalle").open)$("detalle").showModal();
  $("detalle-lineas").replaceChildren();
  $("detalle-lineas-estado").textContent="Cargando productos…";
  cargarLineas(f);
  try {
    const r=await consultar(`/api/facturas/${encodeURIComponent(f.id)}/imagen-url`);
    if(facturaActual?.id===f.id&&/^https?:\/\//i.test(r.url)){$("imagen").href=r.url;$("imagen").hidden=false;}
  }catch{/* Una imagen inaccesible no impide corregir el encabezado. */}
}
async function cargarLineas(f) {
  try {
    const detalle=Array.isArray(f.items)?f:await consultar(`/api/facturas/${encodeURIComponent(f.id)}/detalle`);
    if(facturaActual?.id!==f.id)return;
    const lineas=[...(detalle.items||[]).map(i=>({...i,esCargo:false})),...(detalle.cargos||[]).map(i=>({...i,esCargo:true}))];
    $("detalle-lineas-estado").textContent=lineas.length?`${detalle.items?.length||0} productos · ${detalle.cargos?.length||0} cargos`:"No hay líneas guardadas en el historial de esta factura.";
    const cont=$("detalle-lineas");cont.replaceChildren();
    for(const i of lineas){
      const fila=elemento("div","invoice-line");
      fila.append(elemento("strong","",i.glosa_original||i.nombre_glosa||"Sin descripción"));
      fila.append(elemento("span","",`${i.esCargo?"Cargo":"Producto"} · Cantidad registrada: ${i.cantidad??"—"} · Precio registrado: ${dinero(i.precio_unitario,detalle.moneda||f.moneda||filtros().moneda)}`));
      fila.append(elemento("small","muted",`SKU: ${i.sku_homologado||"Sin asociar"} · ${i.estado||"Sin estado"}`));cont.append(fila);
    }
  }catch(error){if(facturaActual?.id===f.id)$("detalle-lineas-estado").textContent=error.message;}
}
$("cerrar-detalle").addEventListener("click",()=>$("detalle").close());
$("finanzas-form").addEventListener("submit",async event=>{
  event.preventDefault();if(!facturaActual)return;
  const id=facturaActual.id;
  const payload=Object.fromEntries(new FormData(event.target));
  payload.monto_iva=$("usar-desglose").checked?(payload.monto_iva||null):null;
  payload.impuestos_adicionales=$("usar-desglose").checked?leerImpuestos():null;
  if(payload.impuestos_adicionales)payload.impuestos_adicionales.forEach(i=>{if(!i.nombre)i.nombre="Impuesto adicional";});
  for(const campo of ["monto_neto","monto_impuestos","monto_exento","total_factura"])payload[campo]=payload[campo]===""?null:payload[campo];
  for(const campo of ["rut_proveedor","fecha_emision","moneda"])payload[campo]=payload[campo].trim()||null;
  if(payload.moneda)payload.moneda=payload.moneda.toUpperCase();
  $("guardar").disabled=true;$("guardar-estado").textContent="Guardando…";
  try {
    const r=await consultar(`/api/facturas/${encodeURIComponent(id)}/finanzas`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    $("guardar-estado").textContent=r.estado_validacion==="VALIDADO"?"Encabezado validado y guardado. El inventario no se modificó.":"Guardado. Aún hay datos por revisar.";
    $("motivos").replaceChildren(...r.motivos_revision.map(m=>elemento("li","",m.mensaje)));
    await cargar();
  }catch(error){$("guardar-estado").textContent=error.message;}
  finally{$("guardar").disabled=false;}
});
function mensajeChat(texto, clase) {const nodo=elemento("div",`message ${clase}`,texto);$("chat").append(nodo);$("chat").scrollTop=$("chat").scrollHeight;return nodo;}
$("chat-form").addEventListener("submit",async event=>{
  event.preventDefault();if($("enviar").disabled)return;
  const pregunta=$("pregunta").value.trim();if(pregunta.length<3)return;
  mensajeChat(pregunta,"user");$("pregunta").value="";$("enviar").disabled=true;
  const pendiente=mensajeChat("Consultando tus facturas…","bot");
  try{
    const r=await consultar("/api/asistente",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pregunta,...filtros()})});
    pendiente.textContent=r.respuesta;
    for(const f of r.facturas){const b=elemento("button","source-button",`Factura ${f.folio} · ${f.proveedor}`);b.type="button";b.addEventListener("click",()=>abrirFactura(f));pendiente.append(b);}
  }catch(error){pendiente.textContent=error.message;}
  finally{$("enviar").disabled=false;$("chat").scrollTop=$("chat").scrollHeight;}
});
document.querySelectorAll("[data-question]").forEach(b=>b.addEventListener("click",()=>{$("pregunta").value=b.dataset.question;$("chat-form").requestSubmit();}));
$("filtros").addEventListener("submit",event=>{event.preventDefault();cargar();});
$("tres-meses").addEventListener("click",()=>{periodoInicial();cargar();});
periodoInicial();cargar();
