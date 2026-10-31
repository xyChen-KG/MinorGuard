import jadx.api.*;
import com.google.gson.Gson;
import java.io.File;
import java.nio.file.*;
import java.lang.reflect.Field;
import java.util.*;

/** One isolated, read-only JVM. No plugin, disk code-cache, APK changes or GUI state. */
public class IndependentRead {
    public static void main(String[] argv) throws Exception {
        Path out = Paths.get(argv[2]);
        Map<String, Object> metadata = new LinkedHashMap<>();
        JadxArgs args = new JadxArgs();
        args.setInputFile(new File(argv[0]));
        args.setSkipResources(true);
        args.setThreadsCount(4);
        args.setReplaceConsts(false);
        // Keep synthetic callbacks and helper methods visible instead of moving them.
        args.setInlineMethods(false);
        args.setInlineAnonymousClasses(false);
        args.setAllowInlineKotlinLambda(false);
        args.setMoveInnerClasses(false);
        args.setCommentsLevel(CommentsLevel.DEBUG);
        long start = System.nanoTime();
        try (JadxDecompiler jadx = new JadxDecompiler(args)) {
            jadx.load(); // Synchronous; never inspect code before this returns.
            if (jadx.getRoot() == null || jadx.getRoot().getMethodUtils() == null || jadx.getRoot().getClsp() == null) {
                throw new IllegalStateException("Independent analyzer initialization failed");
            }
            boolean passesReady = false;
            for (var pass : jadx.getRoot().getPasses()) {
                if (pass.getClass().getName().endsWith(".AttachMethodDetails")) {
                    Field field = pass.getClass().getDeclaredField("methodUtils");
                    field.setAccessible(true);
                    passesReady = field.get(pass) != null;
                }
            }
            if (!passesReady) throw new IllegalStateException("Independent passes not initialized");
            metadata.put("load_returned", true);
            metadata.put("passes_initialized", true);
            metadata.put("classes", jadx.getRoot().getClasses().size());
            metadata.put("load_seconds", (System.nanoTime() - start) / 1e9);
            JavaClass cls = jadx.searchJavaClassByOrigFullName(argv[1]);
            if (cls == null) cls = jadx.searchJavaClassByAliasFullName(argv[1]);
            if (cls == null) throw new IllegalArgumentException("Class not found in same full APK");
            metadata.put("raw_name", cls.getRawName());
            metadata.put("class_name", cls.getFullName());
            // Save instructions FIRST: Java generation may time out or fail.
            Files.writeString(out.resolve("class.smali"), cls.getSmali());
            try { Files.writeString(out.resolve("class.java"), cls.getCode()); }
            catch (Exception error) { metadata.put("source_error", error.getClass().getSimpleName()); }
            metadata.put("jadx_errors", jadx.getErrorsCount());
        } finally {
            metadata.put("seconds", (System.nanoTime() - start) / 1e9);
            Files.writeString(out.resolve("metadata.json"), new Gson().toJson(metadata));
        }
    }
}
