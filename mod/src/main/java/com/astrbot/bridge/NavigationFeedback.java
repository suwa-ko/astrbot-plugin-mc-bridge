package com.astrbot.bridge;

import com.google.gson.JsonObject;
import java.lang.reflect.*;
import java.util.Arrays;
import java.util.Set;

/** Typed API when present; structurally checked adapter for the installed standalone 1.11.3.
 * Path events are hints tied to the active GoalBlock, never proof of actual arrival. */
final class NavigationFeedback {
    private Object primary,handler,listener,behavior;
    private Field goalField;
    private Method goalGetter;
    private boolean installed,active;
    private String expected="",phase="idle",adapter="unavailable";
    private long events,failures;
    private static Method method(Class<?> type,String returnName,String... parameters)throws ReflectiveOperationException{
        return Arrays.stream(type.getMethods()).filter(m->m.getReturnType().getName().equals(returnName)&&m.getParameterCount()==parameters.length
                &&java.util.stream.IntStream.range(0,parameters.length).allMatch(i->m.getParameterTypes()[i].getName().equals(parameters[i])))
                .findFirst().orElseThrow(()->new NoSuchMethodException(type.getName()+" -> "+returnName));
    }
    private void install()throws ReflectiveOperationException{
        if(installed)return;
        Class<?> callback,event;
        try{
            Class<?> api=Class.forName("baritone.api.BaritoneAPI");Object provider=api.getMethod("getProvider").invoke(null);
            primary=provider.getClass().getMethod("getPrimaryBaritone").invoke(provider);
            handler=primary.getClass().getMethod("getGameEventHandler").invoke(primary);
            behavior=primary.getClass().getMethod("getPathingBehavior").invoke(primary);
            goalGetter=behavior.getClass().getMethod("getGoal");
            callback=Class.forName("baritone.api.event.listener.IGameEventListener");event=Class.forName("baritone.api.event.events.PathEvent");adapter="named-api";
        }catch(ClassNotFoundException standalone){
            Object provider=method(Class.forName("baritone.c"),"baritone.api.IBaritoneProvider").invoke(null);
            primary=method(provider.getClass(),"baritone.d").invoke(provider);
            handler=method(primary.getClass(),"baritone.hn").invoke(primary);
            behavior=method(primary.getClass(),"baritone.fc").invoke(primary);
            goalField=Arrays.stream(behavior.getClass().getFields()).filter(f->f.getType().getName().equals("baritone.by")).findFirst().orElseThrow(NoSuchFieldException::new);
            callback=Class.forName("baritone.bt");event=Class.forName("baritone.bj");adapter="standalone-1.11.3";
        }
        if(!event.isEnum()||Arrays.stream(event.getEnumConstants()).noneMatch(e->((Enum<?>)e).name().equals("CALC_FAILED")))throw new IllegalStateException("unsupported path event schema");
        Class<?> eventClass=event;
        listener=Proxy.newProxyInstance(callback.getClassLoader(),new Class<?>[]{callback},(proxy,m,args)->{
            if(m.getDeclaringClass()==Object.class)return switch(m.getName()){case "toString"->"MinecraftBridgePathObserver";case "hashCode"->System.identityHashCode(proxy);case "equals"->proxy==args[0];default->null;};
            if(args!=null&&args.length==1&&eventClass.isInstance(args[0])&&active&&matchesGoal())accept(((Enum<?>)args[0]).name());
            return null;
        });
        method(handler.getClass(),"void",callback.getName()).invoke(handler,listener);installed=true;
    }
    private boolean matchesGoal(){
        try{Object goal=goalGetter!=null?goalGetter.invoke(behavior):goalField.get(behavior);return goal!=null&&expected.equals(goal.toString());}
        catch(ReflectiveOperationException ex){return false;}
    }
    void begin(int x,int y,int z){
        end();expected="GoalBlock{x="+x+",y="+y+",z="+z+"}";phase="requested";
        try{install();active=true;}catch(ReflectiveOperationException|RuntimeException ex){adapter="unavailable";active=false;}
    }
    void accept(String event){
        if(!active)return;
        if(!Set.of("CALC_STARTED","CALC_FINISHED_NOW_EXECUTING","CALC_FAILED","NEXT_SEGMENT_CALC_STARTED","NEXT_SEGMENT_CALC_FINISHED","CONTINUING_ONTO_PLANNED_NEXT","AT_GOAL","PATH_FINISHED","CANCELED").contains(event))return;
        events++;phase=event;if(event.equals("CALC_FAILED"))failures++;
    }
    void end(){active=false;expected="";phase="idle";}
    JsonObject state(){JsonObject r=new JsonObject();r.addProperty("supported",installed);r.addProperty("active",active);r.addProperty("adapter",adapter);r.addProperty("phase",phase);r.addProperty("events",events);r.addProperty("calc_failures",failures);return r;}
}
