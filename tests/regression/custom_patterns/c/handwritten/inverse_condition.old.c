int foobar();
int barfoo();
void foo();
void bar();

void inverse_condition() {
    if (foobar() > barfoo()) {
        foo();
    } else {
        bar();
    }
}
