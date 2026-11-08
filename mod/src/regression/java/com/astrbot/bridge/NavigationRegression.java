package com.astrbot.bridge;

import baritone.api.BaritoneAPI;

public final class NavigationRegression {
    private static void require(boolean value,String message){if(!value)throw new AssertionError(message);}
    public static void main(String[] args)throws Exception {
        var settings=BaritoneAPI.getSettings();var navigation=new SafeNavigation();
        navigation.begin();
        require(!settings.allowBreak.value&&!settings.allowPlace.value&&settings.allowParkour.value&&settings.allowDiagonalDescend.value&&settings.allowDiagonalAscend.value&&!settings.freeLook.value,"non-destructive movement with small jumps and facing");
        settings.allowBreak.value=true;settings.allowParkour.value=false;settings.freeLook.value=true;navigation.enforce();
        require(!settings.allowBreak.value&&settings.allowParkour.value&&settings.allowDiagonalDescend.value&&settings.allowDiagonalAscend.value&&!settings.freeLook.value,"active protection restores allowed jumps and facing");
        navigation.begin();
        require(!settings.allowBreak.value,"reissuing movement never briefly restores permissions");
        require(navigation.state().get("active").getAsBoolean()&&!navigation.state().get("allowBreak").getAsBoolean()&&navigation.state().get("allowParkour").getAsBoolean(),"runtime reports actual protected settings");
        navigation.restore();
        require(settings.allowBreak.value&&settings.allowPlace.value&&!settings.allowParkour.value&&!settings.allowDiagonalDescend.value&&!settings.allowDiagonalAscend.value&&settings.freeLook.value,"restore preserves original values after repeated begin");
        navigation.restore();require(settings.allowBreak.value,"restore is idempotent");
        navigation.begin(true);
        require(settings.allowBreak.value&&!settings.allowPlace.value&&settings.allowParkour.value&&!settings.freeLook.value,"excavation mode allows breaking only");
        settings.allowBreak.value=false;navigation.enforce();
        require(settings.allowBreak.value,"excavation protection stays enabled");
        navigation.restore();
        require(settings.allowBreak.value&&settings.allowPlace.value&&!settings.allowParkour.value&&settings.freeLook.value,"restore after excavation");
        var snapshot=new java.util.HashMap<net.minecraft.core.BlockPos,Boolean>();
        var cell=new net.minecraft.core.BlockPos(0,64,0);
        require(SurvivalActions.breakTransition(snapshot,cell,true)==0,"first solid observation is not a break");
        require(SurvivalActions.breakTransition(snapshot,cell,false)==1,"solid to air counts one break");
        require(SurvivalActions.breakTransition(snapshot,cell,false)==0,"air to air is not counted again");
        require(SurvivalActions.breakTransition(snapshot,cell,true)==0,"placing a block is not a break");
        require(SurvivalActions.breakTransition(snapshot,cell,false)==1,"breaking a replaced block counts again");
        System.out.println("Navigation guard regression passed; no game actions or fixture classes in production jar");
    }
}
