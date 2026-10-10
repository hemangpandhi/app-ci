/*
 * Javadoc doclet that writes PlantUML class diagrams: one per package plus a package-dependency
 * overview. Uses only the JDK doclet API, so it runs on the in-tree JDK without extra downloads.
 *
 * Options: -d <dir>  -visibility public|protected|package|private  -maxmembers <n>  -smetana
 */
import java.io.IOException;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import javax.lang.model.SourceVersion;
import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.lang.model.element.ExecutableElement;
import javax.lang.model.element.Modifier;
import javax.lang.model.element.PackageElement;
import javax.lang.model.element.TypeElement;
import javax.lang.model.element.VariableElement;
import javax.lang.model.type.DeclaredType;
import javax.lang.model.type.TypeKind;
import javax.lang.model.type.TypeMirror;
import javax.lang.model.util.ElementFilter;
import javax.lang.model.util.Elements;
import jdk.javadoc.doclet.Doclet;
import jdk.javadoc.doclet.DocletEnvironment;
import jdk.javadoc.doclet.Reporter;

public class PlantUmlDoclet implements Doclet {
    private String outDir = "uml";
    private int minVisibility = 1; // 0 public, 1 protected, 2 package, 3 private
    private int maxMembers = 30;
    private boolean smetana = false;
    private Reporter reporter;
    private Elements elements;
    private Set<String> included = new TreeSet<>();

    @Override public void init(Locale locale, Reporter reporter) { this.reporter = reporter; }
    @Override public String getName() { return "PlantUmlDoclet"; }
    @Override public SourceVersion getSupportedSourceVersion() { return SourceVersion.latest(); }

    private abstract class Opt implements Doclet.Option {
        private final String name; private final int args; private final String desc;
        Opt(String name, int args, String desc) { this.name = name; this.args = args; this.desc = desc; }
        @Override public int getArgumentCount() { return args; }
        @Override public String getDescription() { return desc; }
        @Override public Kind getKind() { return Kind.STANDARD; }
        @Override public List<String> getNames() { return List.of(name); }
        @Override public String getParameters() { return args > 0 ? "<value>" : ""; }
    }

    @Override
    public Set<? extends Doclet.Option> getSupportedOptions() {
        Set<Doclet.Option> s = new LinkedHashSet<>();
        s.add(new Opt("-d", 1, "output directory") {
            @Override public boolean process(String o, List<String> a) { outDir = a.get(0); return true; } });
        s.add(new Opt("-visibility", 1, "public|protected|package|private") {
            @Override public boolean process(String o, List<String> a) {
                minVisibility = List.of("public", "protected", "package", "private").indexOf(a.get(0));
                return minVisibility >= 0; } });
        s.add(new Opt("-maxmembers", 1, "max fields/methods shown per class") {
            @Override public boolean process(String o, List<String> a) {
                maxMembers = Integer.parseInt(a.get(0)); return true; } });
        s.add(new Opt("-smetana", 0, "use the built-in layout engine (no Graphviz)") {
            @Override public boolean process(String o, List<String> a) { smetana = true; return true; } });
        return s;
    }

    private int visibility(Element e) {
        Set<Modifier> m = e.getModifiers();
        if (m.contains(Modifier.PUBLIC)) return 0;
        if (m.contains(Modifier.PROTECTED)) return 1;
        if (m.contains(Modifier.PRIVATE)) return 3;
        if (e.getEnclosingElement() != null && e.getEnclosingElement().getKind() == ElementKind.INTERFACE) return 0;
        return 2;
    }

    private static String symbol(int v) { return new String[] {"+", "#", "~", "-"}[v]; }

    private static String alias(String fq) { return fq.replaceAll("[^A-Za-z0-9_]", "_"); }

    private String simpleName(TypeElement t) {
        String pkg = elements.getPackageOf(t).getQualifiedName().toString();
        String fq = t.getQualifiedName().toString();
        return pkg.isEmpty() ? fq : fq.substring(pkg.length() + 1);
    }

    private static String shortType(TypeMirror t) {
        String s = t.toString();
        return s.replaceAll("(?:[a-z_][\\w]*\\.)+([A-Z])", "$1").replace("java.lang.", "");
    }

    /** Included types referenced by `t` (including type arguments such as List<Foo>). */
    private void referenced(TypeMirror t, Set<String> out) {
        if (t.getKind() == TypeKind.ARRAY) {
            referenced(((javax.lang.model.type.ArrayType) t).getComponentType(), out);
        } else if (t.getKind() == TypeKind.DECLARED) {
            DeclaredType d = (DeclaredType) t;
            String fq = ((TypeElement) d.asElement()).getQualifiedName().toString();
            if (included.contains(fq)) out.add(fq);
            for (TypeMirror a : d.getTypeArguments()) referenced(a, out);
        }
    }

    private String keyword(TypeElement t) {
        switch (t.getKind()) {
            case INTERFACE: return "interface";
            case ENUM: return "enum";
            case ANNOTATION_TYPE: return "annotation";
            case RECORD: return "class";
            default: return t.getModifiers().contains(Modifier.ABSTRACT) ? "abstract class" : "class";
        }
    }

    @Override
    public boolean run(DocletEnvironment env) {
        elements = env.getElementUtils();
        Map<String, List<TypeElement>> byPkg = new TreeMap<>();
        for (TypeElement t : ElementFilter.typesIn(env.getIncludedElements())) {
            included.add(t.getQualifiedName().toString());
            byPkg.computeIfAbsent(elements.getPackageOf(t).getQualifiedName().toString(),
                    k -> new ArrayList<>()).add(t);
        }
        Map<String, Set<String>> pkgDeps = new TreeMap<>();
        try {
            Files.createDirectories(Paths.get(outDir));
            for (Map.Entry<String, List<TypeElement>> e : byPkg.entrySet()) {
                pkgDeps.put(e.getKey(), writePackage(e.getKey(), e.getValue()));
            }
            writeOverview(pkgDeps);
        } catch (IOException ex) {
            reporter.print(javax.tools.Diagnostic.Kind.ERROR, "PlantUmlDoclet: " + ex);
            return false;
        }
        return true;
    }

    private PrintWriter open(String name) throws IOException {
        Path p = Paths.get(outDir, name + ".puml");
        return new PrintWriter(Files.newBufferedWriter(p, StandardCharsets.UTF_8));
    }

    private void header(PrintWriter w, String title) {
        w.println("@startuml");
        if (smetana) w.println("!pragma layout smetana");
        w.println("skinparam classAttributeIconSize 0");
        w.println("skinparam shadowing false");
        w.println("skinparam packageStyle rectangle");
        w.println("hide empty members");
        w.println("set namespaceSeparator none");
        w.println("title " + title);
    }

    private Set<String> writePackage(String pkg, List<TypeElement> types) throws IOException {
        Set<String> deps = new TreeSet<>();
        Collections.sort(types, (a, b) -> a.getQualifiedName().toString().compareTo(b.getQualifiedName().toString()));
        Set<String> externals = new TreeSet<>();
        List<String> relations = new ArrayList<>();
        try (PrintWriter w = open(pkg)) {
            header(w, pkg);
            w.println("package \"" + pkg + "\" {");
            for (TypeElement t : types) {
                String fq = t.getQualifiedName().toString();
                w.println("  " + keyword(t) + " \"" + simpleName(t) + "\" as " + alias(fq) + " {");
                int shown = 0;
                for (VariableElement f : ElementFilter.fieldsIn(t.getEnclosedElements())) {
                    if (visibility(f) > minVisibility) continue;
                    if (t.getKind() == ElementKind.ENUM && f.getKind() == ElementKind.ENUM_CONSTANT) {
                        if (shown++ < maxMembers) w.println("    " + f.getSimpleName());
                        continue;
                    }
                    if (shown++ < maxMembers) {
                        String st = f.getModifiers().contains(Modifier.STATIC) ? "{static} " : "";
                        w.println("    " + symbol(visibility(f)) + st + f.getSimpleName() + " : " + shortType(f.asType()));
                    }
                    Set<String> refs = new TreeSet<>();
                    referenced(f.asType(), refs);
                    for (String r : refs) {
                        if (!r.equals(fq) && !f.getModifiers().contains(Modifier.STATIC)) {
                            relations.add(alias(fq) + " --> " + alias(r) + " : " + f.getSimpleName());
                            externals.add(r);
                        }
                    }
                }
                for (ExecutableElement m : ElementFilter.methodsIn(t.getEnclosedElements())) {
                    if (visibility(m) > minVisibility) continue;
                    Set<String> refs = new TreeSet<>();
                    referenced(m.getReturnType(), refs);
                    for (VariableElement p : m.getParameters()) referenced(p.asType(), refs);
                    for (String r : refs) deps.add(elements.getPackageOf(elements.getTypeElement(r)).getQualifiedName().toString());
                    if (shown++ >= maxMembers) continue;
                    StringBuilder sig = new StringBuilder();
                    for (VariableElement p : m.getParameters()) {
                        if (sig.length() > 0) sig.append(", ");
                        sig.append(shortType(p.asType()));
                    }
                    String mods = (m.getModifiers().contains(Modifier.STATIC) ? "{static} " : "")
                            + (m.getModifiers().contains(Modifier.ABSTRACT) && t.getKind() != ElementKind.INTERFACE ? "{abstract} " : "");
                    w.println("    " + symbol(visibility(m)) + mods + m.getSimpleName() + "(" + sig + ") : " + shortType(m.getReturnType()));
                }
                if (shown > maxMembers) w.println("    .. " + (shown - maxMembers) + " more ..");
                w.println("  }");
                // Inheritance (only to types we document; external supertypes become stereotypes).
                TypeMirror sup = t.getSuperclass();
                if (sup.getKind() == TypeKind.DECLARED) {
                    String s = ((TypeElement) ((DeclaredType) sup).asElement()).getQualifiedName().toString();
                    if (included.contains(s)) { relations.add(alias(s) + " <|-- " + alias(fq)); externals.add(s); }
                    else if (!s.equals("java.lang.Object") && !s.equals("java.lang.Enum") && !s.equals("java.lang.Record"))
                        relations.add("' " + simpleName(t) + " extends external " + s);
                }
                for (TypeMirror i : t.getInterfaces()) {
                    String s = ((TypeElement) ((DeclaredType) i).asElement()).getQualifiedName().toString();
                    if (included.contains(s)) { relations.add(alias(s) + " <|.. " + alias(fq)); externals.add(s); }
                }
                Element enc = t.getEnclosingElement();
                if (enc instanceof TypeElement) {
                    relations.add(alias(((TypeElement) enc).getQualifiedName().toString()) + " +-- " + alias(fq));
                }
            }
            w.println("}");
            // Types from other packages referenced by relations are declared outside the package box.
            for (String x : externals) {
                TypeElement te = elements.getTypeElement(x);
                if (te == null) continue;
                String xp = elements.getPackageOf(te).getQualifiedName().toString();
                if (xp.equals(pkg)) continue;
                deps.add(xp);
                w.println(keyword(te) + " \"" + xp + "." + simpleName(te) + "\" as " + alias(x) + " #EEEEEE");
            }
            for (String r : new LinkedHashSet<>(relations)) w.println(r);
            w.println("@enduml");
        }
        deps.remove(pkg);
        return deps;
    }

    private void writeOverview(Map<String, Set<String>> pkgDeps) throws IOException {
        try (PrintWriter w = open("overview")) {
            header(w, "Package dependencies");
            w.println("left to right direction");
            for (String p : pkgDeps.keySet()) w.println("package \"" + p + "\" as " + alias(p) + " {\n}");
            for (Map.Entry<String, Set<String>> e : pkgDeps.entrySet()) {
                for (String d : e.getValue()) {
                    if (pkgDeps.containsKey(d)) w.println(alias(e.getKey()) + " ..> " + alias(d));
                }
            }
            w.println("@enduml");
        }
    }
}
