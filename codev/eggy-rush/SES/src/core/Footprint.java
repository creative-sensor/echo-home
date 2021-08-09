/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package core;

/**
 *
 * @author Creativ
 */
public class Footprint {
    int r; // row
    int c; // column

    public Footprint(int r, int c) {
        this.r = r;
        this.c = c;
    }
    
    
    public int Row(){
        return r;
    }
    
    public int Column(){
        return c;
    }
    
    public void setRow(int i){
        this.r = i;
    }
    
    public void setColumn(int i){
        this.c = i;
    }
}
