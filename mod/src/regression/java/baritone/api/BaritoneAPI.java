package baritone.api;

/** Test-only settings fixture; bridgeChecks output is never included in the Mod jar. */
public final class BaritoneAPI {
    public static final class Setting<T> {public T value;public Setting(T initial){value=initial;}}
    public static final class Settings {
        public final Setting<Boolean> allowBreak=new Setting<>(true);
        public final Setting<Boolean> allowPlace=new Setting<>(true);
        public final Setting<Boolean> allowParkour=new Setting<>(false);
        public final Setting<Boolean> allowDiagonalDescend=new Setting<>(false);
        public final Setting<Boolean> allowDiagonalAscend=new Setting<>(false);
        public final Setting<Boolean> freeLook=new Setting<>(true);
    }
    private static final Settings SETTINGS=new Settings();
    public static Settings getSettings(){return SETTINGS;}
}
