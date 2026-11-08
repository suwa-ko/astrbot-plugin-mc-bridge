package com.astrbot.bridge;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.LinkedHashMap;
import java.util.Map;
import com.google.gson.JsonObject;

/** Scope Baritone's terrain-changing options to a non-destructive journey. */
final class SafeNavigation {
    private final Map<Object,Object> previous = new LinkedHashMap<>();
    private final Map<Object,Field> fields = new LinkedHashMap<>();
    private final Map<Object,String> names = new LinkedHashMap<>();
    private boolean allowBreak=false;
    void begin() throws ReflectiveOperationException {begin(false);}
    void begin(boolean allowBreak) throws ReflectiveOperationException {
        this.allowBreak=allowBreak;
        if(!previous.isEmpty()) {
            enforce();
            return;
        }
        Object settings;
        try { settings=Class.forName("baritone.api.BaritoneAPI").getMethod("getSettings").invoke(null); }
        catch(ClassNotFoundException standalone) {
            settings=null;
            // 1.11.3 standalone keeps setting names, but obfuscates API class and method names.
            for(var method:Class.forName("baritone.a").getDeclaredMethods()) {
                if(!Modifier.isStatic(method.getModifiers())||method.getParameterCount()!=0)continue;
                try {
                    method.getReturnType().getField("allowBreak");method.getReturnType().getField("allowPlace");
                    settings=method.invoke(null);break;
                } catch(NoSuchFieldException ignore) {}
            }
            if(settings==null)throw new ClassNotFoundException("supported Baritone settings API unavailable");
        }
        try {
            for (String key : new String[]{"allowBreak", "allowPlace", "allowParkour", "allowDiagonalDescend", "allowDiagonalAscend", "freeLook"}) {
                Object setting = settings.getClass().getField(key).get(settings);
                Field value = settingValue(setting);
                previous.put(setting, value.get(setting));
                fields.put(setting,value);
                names.put(setting,key);
                value.set(setting, key.equals("allowBreak") ? allowBreak : (key.equals("allowParkour") || key.equals("allowDiagonalDescend") || key.equals("allowDiagonalAscend")));
            }
        } catch (ReflectiveOperationException ex) {
            restore();
            throw ex;
        }
    }
    void enforce() throws ReflectiveOperationException {
        for(Object setting:previous.keySet()) {
            String name=names.get(setting);
            boolean desired=name.equals("allowBreak") ? allowBreak : (name.equals("allowParkour") || name.equals("allowDiagonalDescend") || name.equals("allowDiagonalAscend"));
            fields.get(setting).set(setting,desired);
        }
    }
    JsonObject state() {
        JsonObject result=new JsonObject();result.addProperty("active",!previous.isEmpty());
        for(Object setting:previous.keySet()) {
            try{result.addProperty(names.get(setting),Boolean.TRUE.equals(fields.get(setting).get(setting)));}
            catch(ReflectiveOperationException ex){result.addProperty("error","settings unavailable");}
        }
        return result;
    }
    void restore() {
        for (var entry : previous.entrySet()) {
            try { fields.get(entry.getKey()).set(entry.getKey(),entry.getValue()); }
            catch (ReflectiveOperationException ex) { AstrBotBridge.LOGGER.warn("[astrbot-bridge] restore navigation option failed"); }
        }
        previous.clear();
        fields.clear();
        names.clear();
        allowBreak=false;
    }
    private Field settingValue(Object setting) throws ReflectiveOperationException {
        try { return setting.getClass().getField("value"); }
        catch(NoSuchFieldException standalone) {
            Field found=null;
            for(Field field:setting.getClass().getFields())
                if(!Modifier.isFinal(field.getModifiers())&&!Modifier.isStatic(field.getModifiers())&&!field.getType().isPrimitive()&&field.get(setting) instanceof Boolean){
                    if(found!=null)throw new NoSuchFieldException("ambiguous standalone setting value");found=field;
                }
            if(found==null)throw standalone;return found;
        }
    }
}
